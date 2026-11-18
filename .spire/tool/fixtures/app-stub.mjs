// app-stub.mjs: a stand-in for GitHub, for the tests of app-push.mjs: the App's API (the installation of a repository, an installation access token) and the git smart-HTTP remote of the repositories
// under `root` (the real `git http-backend`), where a push needs the token's Basic header. It checks the JWT the way GitHub does (RS256 signature against the test's own public key, iss, iat, exp <= 10 min) and
// RECORDS what it was given, so a test can say what was used. Nothing here is a real key or a real endpoint: the test generates its key.
import { spawn } from "node:child_process";
import { createPublicKey, createVerify } from "node:crypto";
import { createServer } from "node:http";
import { gunzipSync } from "node:zlib";

/**
 * startStub({ root, publicKey, appId, installationId, token, repos, statuses, requireStatuses }) -> { url, calls, close }. `repos`: the "owner/repo" names the app is installed on. `root/<owner>/<repo>.git` are bare repositories.
 * Commit statuses (the spike's protocol, janus-platform#1009): POST/GET /repos/o/r/statuses/<sha> and GET /repos/o/r/commits/<sha>/status, authenticated by the installation token. `statuses` is what the INSTALLATION may do:
 *   "write" (default) | "none" (the app has no statuses permission: a token asked with statuses is refused 422, as GitHub does) | "forbidden" (the token is made, the POST is refused 403).
 * `requireStatuses: ["land/checks", ...]` is the ruleset: a push to refs/heads/main is refused (403) unless each context is `success` on the pushed sha, posted by this app.
 * `calls.events` is the ORDER of everything that mattered, one entry each: { type: "mint", permissions } | { type: "ref", ref, old, new } (a ref the git remote accepted; new is all zeros for a delete) | { type: "status", sha, context, state, description, target_url }.
 */
export async function startStub({ root, publicKey, appId = "5803159", installationId = 4242, token = "stub-installation-token-0123456789abcdef", repos = [], statuses = "write", requireStatuses = null }) {
  const calls = { api: [], git: [], jwtErrors: [], events: [], statuses: {} };
  const ZERO = "0".repeat(40);
  const bearer = `Bearer ${token}`;
  /** The ref updates of a receive-pack request: its pkt-lines up to the flush, `old new ref`. */
  const commandsOf = (body, encoding) => {
    const buf = encoding === "gzip" ? gunzipSync(body) : body;
    const out = [];
    for (let at = 0; at + 4 <= buf.length; ) {
      const n = Number.parseInt(buf.subarray(at, at + 4).toString(), 16);
      if (!n) break;
      const [line] = buf.subarray(at + 4, at + n).toString().split("\0");
      const [old, nw, ref] = line.trim().split(" ");
      out.push({ old, new: nw, ref });
      at += n;
    }
    return out;
  };
  const pub = createPublicKey(publicKey);
  const jwtOk = (header) => {
    const m = /^Bearer ([\w-]+)\.([\w-]+)\.([\w-]+)$/.exec(header ?? "");
    if (!m) return "no bearer JWT";
    const claims = JSON.parse(Buffer.from(m[2], "base64url"));
    if (!createVerify("RSA-SHA256").update(`${m[1]}.${m[2]}`).verify(pub, Buffer.from(m[3], "base64url"))) return "bad signature";
    if (JSON.parse(Buffer.from(m[1], "base64url")).alg !== "RS256") return "not RS256";
    const now = Date.now() / 1000;
    if (claims.iss !== appId) return "wrong iss";
    if (claims.iat > now || claims.exp <= now || claims.exp - claims.iat > 600) return `bad times ${claims.iat} ${claims.exp}`;
    return null;
  };
  const server = createServer(async (req, res) => {
    const body = await new Promise((ok) => { const c = []; req.on("data", (d) => c.push(d)); req.on("end", () => ok(Buffer.concat(c))); });
    const url = new URL(req.url, "http://x");
    const json = (code, o) => { res.writeHead(code, { "Content-Type": "application/json" }); res.end(JSON.stringify(o)); };
    let m;
    if (url.pathname === "/app") return jwtOk(req.headers.authorization) ? json(401, { message: "bad jwt" }) : json(200, { slug: "stub-app", name: "Stub App" });
    if ((m = /^\/repos\/([^/]+)\/([^/]+)\/installation$/.exec(url.pathname))) {
      const bad = jwtOk(req.headers.authorization);
      calls.api.push({ method: req.method, path: url.pathname });
      if (bad) { calls.jwtErrors.push(bad); return json(401, { message: "A JSON web token could not be decoded" }); }
      return repos.includes(`${m[1]}/${m[2]}`) ? json(200, { id: installationId }) : json(404, { message: "Not Found" });
    }
    if ((m = /^\/app\/installations\/(\d+)\/access_tokens$/.exec(url.pathname)) && req.method === "POST") {
      const bad = jwtOk(req.headers.authorization);
      const asked = body.length ? JSON.parse(body) : {};
      calls.api.push({ method: "POST", path: url.pathname, asked });
      if (bad) { calls.jwtErrors.push(bad); return json(401, { message: "A JSON web token could not be decoded" }); }
      if (Number(m[1]) !== installationId) return json(404, { message: "Not Found" });
      calls.events.push({ type: "mint", permissions: asked.permissions });
      if (asked.permissions?.statuses && statuses === "none") return json(422, { message: "The permissions requested are not granted to this installation." });
      return json(201, { token, expires_at: new Date(Date.now() + 3600_000).toISOString(), repository_selection: "selected", repositories: (asked.repositories ?? []).map((name) => ({ name })), permissions: asked.permissions });
    }
    if ((m = /^\/repos\/([^/]+)\/([^/]+)\/statuses\/([0-9a-f]{40})$/.exec(url.pathname)) && req.method === "POST") {
      if (req.headers.authorization !== bearer) return json(401, { message: "Bad credentials" });
      if (statuses !== "write") return json(403, { message: "Resource not accessible by integration" });
      const b = JSON.parse(body.toString() || "{}");
      if (!["error", "failure", "pending", "success"].includes(b.state) || !b.context) return json(422, { message: "Validation Failed" });
      if ((b.description ?? "").length > 140) return json(422, { message: "description is too long (maximum is 140 characters)" });
      const entry = { sha: m[3], context: b.context, state: b.state, description: b.description ?? null, target_url: b.target_url ?? null, app: appId };
      (calls.statuses[m[3]] ??= []).push(entry);
      calls.events.push({ type: "status", ...entry });
      return json(201, { id: calls.events.length, context: b.context, state: b.state, description: b.description ?? null, target_url: b.target_url ?? null });
    }
    if ((m = /^\/repos\/([^/]+)\/([^/]+)\/commits\/([^/]+)\/status$/.exec(url.pathname)) && req.method === "GET") {
      if (req.headers.authorization !== bearer) return json(401, { message: "Bad credentials" });
      calls.api.push({ method: "GET", path: url.pathname });
      const latest = new Map(); // one per context, the newest, as the combined status shows it
      for (const e of calls.statuses[m[3]] ?? []) latest.set(e.context, e);
      const list = [...latest.values()].map(({ context, state, description, target_url }) => ({ context, state, description, target_url }));
      return json(200, { state: list.length && list.every((e) => e.state === "success") ? "success" : "pending", sha: m[3], total_count: list.length, statuses: list });
    }
    if (/\.git\//.test(url.pathname)) {
      // git smart HTTP: reading is open, receive-pack needs the app's token as the Basic header `x-access-token:<token>`
      const push = url.searchParams.get("service") === "git-receive-pack" || url.pathname.endsWith("/git-receive-pack");
      const auth = req.headers.authorization ?? null;
      calls.git.push({ method: req.method, path: url.pathname, push, auth });
      if (push && auth !== `Basic ${Buffer.from(`x-access-token:${token}`).toString("base64")}`) { res.writeHead(401, { "WWW-Authenticate": 'Basic realm="stub"' }); return res.end(); }
      const cmds = push && req.method === "POST" ? commandsOf(body, req.headers["content-encoding"]) : [];
      if (requireStatuses && cmds.some((c) => c.ref === "refs/heads/main" && !requireStatuses.every((ctx) => (calls.statuses[c.new] ?? []).some((e) => e.context === ctx && e.state === "success" && e.app === appId)))) {
        calls.events.push({ type: "refused", ref: "refs/heads/main", reason: "required status checks not set" });
        res.writeHead(403, { "Content-Type": "text/plain" });
        return res.end("Required status checks were not set by the expected GitHub apps");
      }
      const cgi = spawn("git", ["http-backend"], {
        env: { PATH: process.env.PATH, GIT_PROJECT_ROOT: root, GIT_HTTP_EXPORT_ALL: "1", REQUEST_METHOD: req.method, PATH_INFO: url.pathname, QUERY_STRING: url.search.slice(1), CONTENT_TYPE: req.headers["content-type"] ?? "", CONTENT_LENGTH: String(body.length), REMOTE_USER: "app", REMOTE_ADDR: "127.0.0.1", ...(req.headers["content-encoding"] ? { HTTP_CONTENT_ENCODING: req.headers["content-encoding"] } : {}) },
      });
      cgi.stdin.end(body);
      const out = await new Promise((ok) => { const c = []; cgi.stdout.on("data", (d) => c.push(d)); cgi.on("close", () => ok(Buffer.concat(c))); });
      const cut = out.indexOf("\r\n\r\n");
      const head = out.subarray(0, cut).toString().split("\r\n");
      const status = /^Status: (\d+)/m.exec(head.join("\n"));
      res.writeHead(status ? Number(status[1]) : 200, Object.fromEntries(head.filter((h) => !h.startsWith("Status:")).map((h) => [h.slice(0, h.indexOf(":")), h.slice(h.indexOf(":") + 2)])));
      for (const c of cmds) calls.events.push({ type: "ref", ref: c.ref, old: c.old, new: c.new });
      return res.end(out.subarray(cut + 4));
    }
    json(404, { message: "Not Found" });
  });
  await new Promise((ok) => server.listen(0, "127.0.0.1", ok));
  return { url: `http://127.0.0.1:${server.address().port}`, calls, close: () => new Promise((ok) => { server.close(ok); server.closeAllConnections?.(); }) };
}
