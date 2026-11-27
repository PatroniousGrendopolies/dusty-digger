// Dusty Digger list finder: reads the "Lists" sidebar of a Discogs release page with an
// ordinary Google Chrome (own profile, local debugging port), at human pace.
// GET /lists?release=<digits>  (header X-DD-Key required)  ->  {lists:[{id,name,url}], status}
const http = require("http");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawn } = require("child_process");
const crypto = require("crypto");

const HOME = os.homedir();
const KEY = fs.readFileSync(path.join(HOME, ".config/dd-listfinder/key"), "utf8").trim();
const PORT = +process.env.FINDER_PORT || 8787;
const CDP_PORT = 9333;
const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const PROFILE = path.join(HOME, "Library/Application Support/DD List Finder Chrome");
const CACHE_FILE = path.join(HOME, ".config/dd-listfinder/cache.json");
const MIN_GAP = 8000;           // ms between Discogs page loads
const TTL = 7 * 24 * 3600e3;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (...a) => console.log(new Date().toISOString(), ...a);

let cache = {};
try { cache = JSON.parse(fs.readFileSync(CACHE_FILE, "utf8")); } catch (e) {}
function saveCache() { try { fs.writeFileSync(CACHE_FILE, JSON.stringify(cache)); } catch (e) {} }

async function cdpJson(p, opts) {
  const r = await fetch(`http://127.0.0.1:${CDP_PORT}${p}`, { signal: AbortSignal.timeout(3000), ...opts });
  return r.json();
}

async function ensureChrome() {
  try { await cdpJson("/json/version"); return; } catch (e) {}
  log("starting Chrome");
  fs.mkdirSync(PROFILE, { recursive: true });
  const p = spawn(CHROME, [
    `--user-data-dir=${PROFILE}`, `--remote-debugging-port=${CDP_PORT}`, "--remote-debugging-address=127.0.0.1",
    "--no-first-run", "--no-default-browser-check", "--start-minimized", "about:blank",
  ], { detached: true, stdio: "ignore" });
  p.unref();
  for (let i = 0; i < 30; i++) {
    await sleep(1000);
    try { await cdpJson("/json/version"); return; } catch (e) {}
  }
  throw new Error("chrome did not start");
}

// One reused tab. A fresh websocket per command: Discogs/Cloudflare reloads can swap the page target.
async function cdp(method, params = {}) {
  let targets = await cdpJson("/json");
  let t = targets.find((x) => x.type === "page");
  if (!t) t = await cdpJson("/json/new?about:blank", { method: "PUT" });
  const ws = new WebSocket(t.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  try {
    return await new Promise((res, rej) => {
      ws.onmessage = (m) => { const d = JSON.parse(m.data); if (d.id === 1) (d.error ? rej(new Error(d.error.message)) : res(d.result)); };
      ws.onclose = () => rej(new Error("socket closed"));
      ws.send(JSON.stringify({ id: 1, method, params }));
      setTimeout(() => rej(new Error("cdp timeout " + method)), 30000);
    });
  } finally { try { ws.close(); } catch (e) {} }
}
async function evaluate(expression) {
  const r = await cdp("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
  return r.result && r.result.value;
}
async function evalRetry(expression, tries = 4) {
  for (let i = 0; ; i++) {
    try { return await evaluate(expression); }
    catch (e) { if (i >= tries) throw e; await sleep(2500); }
  }
}

const READ = `(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const start = location.pathname;
  if (!/^\\/(?:[a-z]{2}\\/)?release\\//.test(start)) return { heading: false, lists: [], note: "not a release page" };
  window.scrollTo(0, document.body.scrollHeight / 2);
  await sleep(4000);
  const heading = () => [...document.querySelectorAll("h2")].find((x) => x.textContent.trim() === "Lists");
  let h = heading();
  if (!h) { window.scrollTo(0, document.body.scrollHeight * 0.75); await sleep(3000); h = heading(); }
  if (!h) return { heading: false, lists: [] };
  // The section is the nearest ancestor that also holds the section's other content (links or an add button).
  const box = () => { let n = h.parentElement; for (let i = 0; i < 4 && n; i++, n = n.parentElement) { if (n.querySelectorAll("h2").length > 1) break; if (n.querySelectorAll("a[href*='/lists/']").length) return n; } return null; };
  // Only a real button may be clicked (never a link: the page must stay on this release).
  let b0 = box();
  const more = b0 && [...b0.querySelectorAll("button")].find((b) => /more|all/i.test(b.textContent.trim()) && !/add/i.test(b.textContent));
  if (more) { more.click(); await sleep(3000); }
  const b1 = box();
  const seen = new Set(), lists = [];
  if (b1 && location.pathname === start) for (const a of b1.querySelectorAll("a[href*='/lists/']")) {
    const m = a.href.match(/\\/lists\\/(?:[^/]+\\/)?(\\d+)/);
    if (!m || seen.has(m[1])) continue;
    const name = a.textContent.trim();
    if (!name) continue;
    seen.add(m[1]);
    lists.push({ id: +m[1], name, url: a.href.split("?")[0] });
  }
  return { heading: true, clickedMore: !!more, lists };
})()`;

let lastLoad = 0;
async function fetchLists(release) {
  await ensureChrome();
  const wait = lastLoad + MIN_GAP - Date.now();
  if (wait > 0) await sleep(wait);
  lastLoad = Date.now();
  await cdp("Page.navigate", { url: `https://www.discogs.com/release/${release}` });
  const title = async () => { try { return String((await evaluate("document.title")) || ""); } catch (e) { return ""; } };
  await sleep(6000);
  let t = await title();
  const blocked = (x) => !x || /just a moment|attention required|verify|^discogs$/i.test(x) && !/\[r\d+\]/.test(x);
  if (blocked(t)) {
    log("challenge page, waiting up to 20s");
    for (let i = 0; i < 20 && blocked(t); i++) { await sleep(1000); t = await title(); }
    if (blocked(t)) return { status: "challenge", lists: [] };
  }
  await sleep(2000);
  const r = await evalRetry(READ);
  log("release", release, "title", JSON.stringify(t.slice(0, 60)), "lists", r && r.lists.length, "heading", r && r.heading);
  if (!r || !r.heading) return { status: "error", lists: [] };
  return { status: r.lists.length ? "ok" : "none", lists: r.lists };
}

let queue = Promise.resolve();
function lookup(release) {
  const hit = cache[release];
  if (hit && Date.now() - hit.at < TTL) return Promise.resolve({ ...hit.data, cached: true });
  const job = queue.then(async () => {
    const again = cache[release];
    if (again && Date.now() - again.at < TTL) return { ...again.data, cached: true };
    let data;
    try { data = await fetchLists(release); }
    catch (e) { log("error", e.message); data = { status: "error", lists: [] }; }
    if (data.status === "ok" || data.status === "none") { cache[release] = { at: Date.now(), data }; saveCache(); }
    return data;
  });
  queue = job.catch(() => {});
  return job;
}

function authed(req) {
  const got = Buffer.from(String(req.headers["x-dd-key"] || ""));
  const want = Buffer.from(KEY);
  return got.length === want.length && crypto.timingSafeEqual(got, want);
}

http.createServer(async (req, res) => {
  const send = (code, obj) => { res.writeHead(code, { "content-type": "application/json" }); res.end(JSON.stringify(obj)); };
  if (!authed(req)) return send(401, { error: "unauthorized" });
  const u = new URL(req.url, "http://x");
  if (u.pathname === "/health") return send(200, { ok: true });
  if (u.pathname !== "/lists") return send(404, { error: "not found" });
  const release = u.searchParams.get("release") || "";
  if (!/^\d{1,10}$/.test(release)) return send(400, { error: "release must be digits" });
  send(200, await lookup(release));
}).listen(PORT, "127.0.0.1", () => log("list finder on", PORT));
