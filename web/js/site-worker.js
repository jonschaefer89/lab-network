// A lab's shared read-only copy has no server. This worker runs the app's Python in the browser with Pyodide,
// on the snapshot of the lab's workspace that `labnetwork.py site` packed into data/, and answers the page's
// API requests (see api.js) with the same routes the local server uses (labnetwork/browser.py).
/* global importScripts, loadPyodide */
// Paths are relative to the copy's folder, so it works at a domain's root or in a subfolder.
const ROOT = new URL("../", self.location.href);
const at = (path) => new URL(path, ROOT).href;

let backend = null;
const ready = boot().catch((err) => {
  postMessage({ type: "failed", error: String((err && err.message) || err) });
  throw err;
});

function progress(text) {
  postMessage({ type: "progress", text });
}

async function bytes(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Could not load ${url} (${res.status})`);
  const buf = new Uint8Array(await res.arrayBuffer());
  if (buf[0] === 0x1f && buf[1] === 0x8b) {          // gzip, unless the server already unpacked it
    const stream = new Blob([buf]).stream().pipeThrough(new DecompressionStream("gzip"));
    return new Uint8Array(await new Response(stream).arrayBuffer());
  }
  return buf;
}

async function boot() {
  progress("Starting Python in your browser…");
  const manifest = JSON.parse(new TextDecoder().decode(await bytes(at("data/manifest.json"))));
  importScripts(at(manifest.pyodide + "pyodide.js"));        // throws, and so fails the start, if it cannot load
  const pyodideLoading = loadPyodide({ indexURL: at(manifest.pyodide) });
  const total = manifest.files.reduce((n, f) => n + f.bytes, 0);
  let loaded = 0;
  const files = Promise.all(manifest.files.map(async (f) => {
    const data = await bytes(at(f.url));
    loaded += f.bytes;
    progress(`Loading the lab's data (${Math.round((100 * loaded) / Math.max(total, 1))}%)…`);
    return [f.path, data];
  }));
  const [pyodide, code, data] = await Promise.all([pyodideLoading, bytes(at("py/labnetwork.zip")), files]);
  pyodide.unpackArchive(code, "zip", { extractDir: "/app" });
  for (const [path, content] of data) {
    pyodide.FS.mkdirTree(path.slice(0, path.lastIndexOf("/")));
    pyodide.FS.writeFile(path, content);
  }
  progress("Opening the notebook…");
  pyodide.globals.set("workspace_path", manifest.workspace);
  pyodide.runPython([
    "import sys",
    "sys.path.insert(0, '/app')",
    "from labnetwork.browser import Backend",
    "backend = Backend(workspace_path)",
  ].join("\n"));
  backend = pyodide.globals.get("backend");
  postMessage({ type: "ready" });
}

onmessage = async (e) => {
  const { id, method, path, query } = e.data;
  let out;
  try {
    await ready;
    out = backend.handle(method, path, JSON.stringify(query || {}));
  } catch (err) {
    out = JSON.stringify({ status: 500, body: { error: String((err && err.message) || err) } });
  }
  postMessage({ type: "reply", id, out });
};
