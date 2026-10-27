// Thin client for the local JSON API.
//
// A lab's shared read-only copy (built by `labnetwork.py site`) has no server: <html data-backend="browser">
// sends the same requests to site-worker.js, which answers them with the app's Python running in the browser.

let actor = "";

export function setActor(name) {
  actor = name || "";
}

export class ApiError extends Error {
  constructor(status, message, details) {
    super(message);
    this.status = status;
    this.details = details;
  }
}

async function handle(res) {
  let body = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  if (!res.ok) {
    throw new ApiError(res.status, (body && body.error) || `Request failed (${res.status})`, body && body.details);
  }
  return body;
}

export function qs(params) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params || {})) {
    if (v === undefined || v === null || v === "") continue;
    p.set(k, String(v));
  }
  const s = p.toString();
  return s ? "?" + s : "";
}

export async function get(path, params) {
  if (IN_BROWSER) return inBrowser(path, params);
  const res = await fetch(path + qs(params), { headers: { Accept: "application/json" } });
  return handle(res);
}

export async function send(method, path, body) {
  if (IN_BROWSER) throw new ApiError(403, "This is the lab's read-only copy, so nothing can be changed here.");
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", Accept: "application/json", "X-LabNetwork-Actor": encodeURIComponent(actor) },
    body: JSON.stringify(body || {}),
  });
  return handle(res);
}

// ---------------------------------------------------------------- the shared copy's in-browser backend

const IN_BROWSER = document.documentElement.dataset.backend === "browser";
let worker = null;
let failure = null;           // why the copy could not start; every request then fails with it
let seq = 0;
const waiting = new Map();

function inBrowser(path, params) {
  if (failure) return Promise.reject(new ApiError(503, failure));
  if (!worker) startWorker();
  const [route, search] = path.split("?");
  const query = Object.fromEntries(new URLSearchParams(search || ""));
  for (const [k, v] of Object.entries(params || {})) {
    if (v !== undefined && v !== null && v !== "") query[k] = String(v);
  }
  return new Promise((resolve, reject) => {
    seq += 1;
    waiting.set(seq, { resolve, reject });
    worker.postMessage({ id: seq, method: "GET", path: route, query });
  });
}

function startWorker() {
  const notice = document.createElement("div");
  notice.className = "site-loading";
  notice.setAttribute("role", "status");
  notice.textContent = "Starting…";
  document.body.append(notice);
  const fail = (why) => {
    failure = "This copy could not start: " + why;
    notice.classList.add("site-loading-error");
    notice.textContent = failure;
    for (const p of waiting.values()) p.reject(new ApiError(503, failure));
    waiting.clear();
  };
  worker = new Worker(new URL("./site-worker.js", import.meta.url));
  worker.onerror = (e) => fail((e && e.message) || "the browser stopped its background worker");
  worker.onmessage = (e) => {
    const m = e.data;
    if (m.type === "progress") notice.textContent = m.text;
    else if (m.type === "ready") notice.remove();
    else if (m.type === "failed") fail(m.error);
    else if (m.type === "reply") {
      const p = waiting.get(m.id);
      waiting.delete(m.id);
      if (!p) return;
      const r = JSON.parse(m.out);
      if (r.status >= 400) p.reject(new ApiError(r.status, (r.body && r.body.error) || `Request failed (${r.status})`, r.body && r.body.details));
      else p.resolve(r.body);
    }
  };
}
