// The policy front of the Copilot bridge (ask #2341). Node standard library only. The ONLY listener a pod can reach: the bridge
// (copilot-api 0.7.0) is bound to 127.0.0.1 and this front forwards to it. No secret is read, held or logged here.
// A harness-agnostic OpenAI-compatible metering proxy. Forwards POST /chat/completions and /v1/chat/completions, GET /models and /v1/models;
// answers GET /usage; every other path or method is 404.
// Model policy: the requested id passes through; BRIDGE_BLOCKED_MODELS (substrings, default "opus") and BRIDGE_ALLOWED_MODELS (names, optional) refuse with 403.
// Usage: tokens, nano_aiu (copilot_usage, see spendOf()) and cost_microdollars per day, model and (X-Floor-Session/Repo/Role) session; no daily cap.
// The caller's effort is set as BOTH reasoning.effort and reasoning_effort. Log: one count line per request, never a body, an answer, a header or a token.
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
const list = (v, d) => (v ?? d).split(",").map((x) => x.trim().toLowerCase()).filter(Boolean);
const BLOCKED = list(env.BRIDGE_BLOCKED_MODELS, "opus");
const ALLOWED = list(env.BRIDGE_ALLOWED_MODELS, "");
const SAFE = /^[A-Za-z0-9._:/#-]{1,128}$/;
const hdr = (req, k) => { const v = req.headers[k]; return typeof v === "string" && SAFE.test(v) ? v : "-"; };
const BANNED_TEXT = "model banned from the council: opus";
// Only these fields of a request go on to the bridge (the bridge passes the body to Copilot unchanged): nothing else can name a model or a route.
const FIELDS = ["messages", "stream", "stream_options", "temperature", "top_p", "max_tokens", "tools", "tool_choice", "stop", "seed", "response_format", "presence_penalty", "frequency_penalty"];
const MAX_TOKENS = 64000;
const PATHS = new Set(["/chat/completions", "/v1/chat/completions"]);
const MODELS = new Set(["/models", "/v1/models"]);
const METRICS = ["prompt_tokens", "completion_tokens", "cached_tokens", "nano_aiu", "cost_microdollars"];
const newM = () => ({ requests: 0, nano: 0, prompt_tokens: 0, completion_tokens: 0, cached_tokens: 0, nano_aiu: 0, cost_microdollars: 0 });
const addM = (t, m) => { for (const k of METRICS) t[k] += m[k]; };
const credits = (n) => Number((n / NANO).toFixed(4));
const view = (v) => { const o = { requests: v.requests, credits: credits(v.nano) }; for (const k of METRICS) o[k] = v[k]; return o; };

const days = new Map();   // day -> { requests, refused, models: { id: count } }
const today = () => {
  if (CLOCK_FILE) { try { const d = fs.readFileSync(CLOCK_FILE, "utf8").trim(); if (/^\d{4}-\d{2}-\d{2}$/.test(d)) return d; } catch {} }
  return new Date().toISOString().slice(0, 10);
};
const dayRow = (d) => {
  if (!days.has(d)) { days.set(d, { ...newM(), refused: 0, models: {}, sessions: {} }); for (const k of [...days.keys()].sort().slice(0, -14)) days.delete(k); }
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
// Usage of an answer: a JSON body, or the "data:" lines of a stream (the last usage block wins).
const usageOf = (text) => {
  const out = { raw_nano: null, copilot: false, prompt_tokens: 0, completion_tokens: 0, cached_tokens: 0, cost_microdollars: 0 };
  const take = (j) => {
    if (!j || typeof j !== "object") return;
    const u = j.usage && typeof j.usage === "object" ? j.usage : null;
    if (u) {
      out.prompt_tokens = u.prompt_tokens ?? u.input_tokens ?? 0;
      out.completion_tokens = u.completion_tokens ?? u.output_tokens ?? 0;
      out.cached_tokens = u.cached_tokens ?? u.prompt_tokens_details?.cached_tokens ?? 0;
    }
    const dollars = u?.cost ?? u?.total_cost ?? j.cost;
    const micro = u?.cost_microdollars ?? j.cost_microdollars ?? (typeof dollars === "number" ? Math.round(dollars * 1e6) : 0);
    if (typeof micro === "number" && micro > 0) out.cost_microdollars = micro;
    if (j.copilot_usage) { out.copilot = true; const n = spendOf(j.copilot_usage); if (n !== null) out.raw_nano = n; }
  };
  try { take(JSON.parse(text)); return out; } catch {}
  for (const line of text.split("\n")) {
    if (!line.startsWith("data:")) continue;
    try { take(JSON.parse(line.slice(5))); } catch {}
  }
  return out;
};
const logLine = (entry) => process.stdout.write(JSON.stringify(entry) + "\n");

const send = (res, status, obj) => {
  const text = JSON.stringify(obj);
  res.writeHead(status, { "content-type": "application/json", "content-length": Buffer.byteLength(text) });
  res.end(text);
};

// True when the requested id is blocked or not allowed.
const refused = (id) => {
  const norm = id.toLowerCase();
  return BLOCKED.some((b) => norm.includes(b)) || (ALLOWED.length > 0 && !ALLOWED.includes(norm));
};

const server = http.createServer((req, res) => {
  const started = Date.now();
  const day = today();
  const path = (req.url || "").split("?")[0];
  let done = false;
  const tally = {};   // counters of this request, for its log line
  let who = {};
  const finish = (status, served, bytes) => {
    if (done) return;
    done = true;
    const entry = { ts: new Date().toISOString(), day, method: req.method === "GET" || req.method === "POST" ? req.method : "other", path: PATHS.has(path) || MODELS.has(path) || path === "/usage" ? path : "other", status, model: served, ...who, bytes, ...tally, ms: Date.now() - started };
    logLine(entry);
  };
  const answer = (status, obj, served) => { send(res, status, obj); finish(status, served || "-", 0); };

  if (req.method === "GET" && path === "/usage") {
    const out = { day, days: {} };
    for (const [d, row] of days) {
      const models = {};
      const sessions = {};
      for (const [m, v] of Object.entries(row.models)) models[m] = view(v);
      for (const [k, v] of Object.entries(row.sessions)) sessions[k] = { session: v.session, repo: v.repo, role: v.role, model: v.model, ...view(v) };
      out.days[d] = { ...view(row), refused: row.refused, models, sessions };
    }
    return answer(200, out);
  }
  if (req.method === "GET" && MODELS.has(path)) {
    const up = http.request({ host: UP_HOST, port: UP_PORT, method: "GET", path: "/v1/models", timeout: TIMEOUT_MS }, (ur) => {
      res.writeHead(ur.statusCode || 502, { "content-type": ur.headers["content-type"] || "application/json" });
      ur.pipe(res);
      ur.on("end", () => finish(ur.statusCode || 502, "-", 0));
      ur.on("error", () => { res.destroy(); finish(502, "-", 0); });
    });
    res.on("close", () => up.destroy());
    up.on("timeout", () => { up.destroy(); if (!res.headersSent) send(res, 504, { error: "the bridge did not answer in time" }); finish(504, "-", 0); });
    up.on("error", () => { if (!res.headersSent) send(res, 502, { error: "the bridge is not reachable" }); finish(502, "-", 0); });
    return up.end();
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
    const model = body.model.trim().split("/").pop().trim();
    if (!model) return answer(400, { error: "the body needs a string model and a messages array" });
    const verdict = { model };
    const row = dayRow(day);
    if (refused(model)) { row.refused += 1; return answer(403, { error: BANNED_TEXT }, "refused"); }
    const session = hdr(req, "x-floor-session"), repo = hdr(req, "x-floor-repo"), role = hdr(req, "x-floor-role");
    const mrow = row.models[model] || (row.models[model] = newM());
    const attributed = session !== "-" || repo !== "-" || role !== "-";
    const key = `${session}|${repo}|${role}|${model}`;
    const srow = attributed ? (row.sessions[key] || (row.sessions[key] = { session, repo, role, model, ...newM() })) : null;
    who = { session, repo, role };
    const rows = [row, mrow, ...(srow ? [srow] : [])];
    for (const r of rows) r.requests += 1;
    let payload;
    try {   // a body that cannot be re-serialised (for example nested too deep) is a 400, never a crash
      const effort = (body.reasoning && typeof body.reasoning.effort === "string" ? body.reasoning.effort : (typeof body.reasoning_effort === "string" ? body.reasoning_effort : null));
      const out = { model: verdict.model };
      for (const k of FIELDS) if (Object.prototype.hasOwnProperty.call(body, k)) out[k] = body[k];
      if (typeof out.max_tokens === "number" && out.max_tokens > MAX_TOKENS) out.max_tokens = MAX_TOKENS;
      if (effort) { out.reasoning = { effort }; out.reasoning_effort = effort; }
      const chk = (v, d) => { if (d > 64) throw new RangeError(); if (v && typeof v === "object") for (const x of (Array.isArray(v) ? v : Object.values(v))) chk(x, d + 1); };
      chk(out, 0);
      payload = Buffer.from(JSON.stringify(out));
    } catch { for (const r of rows) r.requests -= 1; return answer(400, { error: "the body cannot be processed" }); }
    const up = http.request({ host: UP_HOST, port: UP_PORT, method: "POST", path: "/chat/completions", timeout: TIMEOUT_MS,
      headers: { "content-type": "application/json", "content-length": payload.length } }, (ur) => {   // no incoming header is forwarded: no Authorization
      res.writeHead(ur.statusCode || 502, { "content-type": ur.headers["content-type"] || "application/json" });
      ur.pipe(res);
      const parts = [];
      let got = 0;
      ur.on("data", (c) => { got += c.length; if (got <= MAX_ANSWER) parts.push(c); });
      ur.on("end", () => {
        if ((ur.statusCode || 502) < 300) {
          const u = got <= MAX_ANSWER ? usageOf(Buffer.concat(parts).toString("utf8")) : usageOf("");
          const nano = u.copilot && u.raw_nano !== null ? u.raw_nano : u.cost_microdollars > 0 ? u.cost_microdollars * 1000 : FALLBACK * NANO;
          if (u.raw_nano === null && u.cost_microdollars === 0) logLine({ ts: new Date().toISOString(), day, event: "unpriced", model: verdict.model, count: 1 });
          const m = { nano, prompt_tokens: u.prompt_tokens, completion_tokens: u.completion_tokens, cached_tokens: u.cached_tokens, nano_aiu: u.raw_nano ?? 0, cost_microdollars: u.cost_microdollars };
          row.nano += nano;
          mrow.nano += nano;
          if (srow) srow.nano += nano;
          for (const r of rows) addM(r, m);
          Object.assign(tally, m);
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
