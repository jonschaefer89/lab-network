"""Build a lab's shared read-only copy: a folder of static files to put behind a sign-in (see docs/hosting.md).

The copy holds the web app, this package, the Pyodide runtime (Python compiled to WebAssembly) and a snapshot of the
notebook. In the browser, Pyodide runs the package and answers the web app's requests with the same code the local
server uses (browser.py), so the map, search, paths and profiles work as they do on the lab's own computer. Nothing in
the copy can be changed; rebuild it to bring it up to date.

The snapshot is the notebook as it is now, written as a new packet with an empty change log. Archived records, earlier
values and the history of edits stay out, as do Slack submissions, imported files, exports, logs and the packet's own
text files. Of the change log, the copy keeps only what the notebook's records do not hold themselves: whether each
follow-up is done, other names a person's record answers to, whose connections were collected, and OpenAlex records
marked as not someone's. None of these carries an earlier value or an archived record.

The workspace is read from a temporary copy of its files, so a server writing to it at the same time is undisturbed.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from typing import Any, Callable, Dict, List, Optional

from .store import CHECKS_NAME, CONFIG_NAME, LOG_NAME, Workspace

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.dirname(HERE)
WEB_DIR = os.path.join(KIT, "web")

# Pyodide, pinned by version and by the SHA-512 checksum npm publishes for it.
PYODIDE_VERSION = "0.29.5"
PYODIDE_URL = f"https://registry.npmjs.org/pyodide/-/pyodide-{PYODIDE_VERSION}.tgz"
PYODIDE_SHA512 = "TkYrUv9m8QmfImADKRmpwZulL48E0uUaBSO+dvRQKyAJc0qh9gu7jn8btyHPh3NZm1r2IH2YrJhIr7Q2B9urAw=="
PYODIDE_FILES = ("pyodide.js", "pyodide.asm.js", "pyodide.asm.wasm", "python_stdlib.zip", "pyodide-lock.json")
PYODIDE_DIR = f"pyodide-{PYODIDE_VERSION}"     # versioned, so browsers may cache it for good

PACKET_FILES = ("lab-roots.json",)    # besides the snapshot's own notebook.json and this kit's schema
WORKSPACE_FILES = ("library.json", "conferences.json", "topic-aliases.json")
SETTINGS_KEPT = ("degreeLimit", "defaultFilters", "demo", "lastRefresh")
MARKER = ".labnetwork-site"
TOP_LEVEL = {MARKER, "_headers", "robots.txt", ".gitignore", "index.html", "css", "js", "img", "py", "data",
             ".DS_Store", "Thumbs.db", "desktop.ini"}         # the last three: what file browsers leave behind
NOTE_KINDS = ("collection", "identity")
BROWSER_ROOT = "/lab"                 # where the snapshot sits in the browser's file system
MAX_FILE = 25 * 1024 * 1024           # Cloudflare Pages' limit for one file

# Response headers for Cloudflare Pages (the _headers file); site-preview applies the same ones.
HEADERS = f"""/*
  Content-Security-Policy: default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self'; style-src 'self'; img-src 'self' https: data:; connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'
  Referrer-Policy: no-referrer
  X-Content-Type-Options: nosniff
  X-Robots-Tag: noindex, nofollow
  Cross-Origin-Opener-Policy: same-origin

/data/*
  Cache-Control: no-cache

/{PYODIDE_DIR}/*
  Cache-Control: public, max-age=31536000, immutable
"""


class SiteError(Exception):
    pass


def build(ws_path: str, out_dir: str, how_to_add: str = "", pyodide_dir: Optional[str] = None,
          force: bool = False, made_at: Optional[str] = None, log: Callable[[str], None] = print) -> Dict[str, Any]:
    """Write the shared copy of the workspace at `ws_path` into `out_dir`. Returns a summary.

    The copy is built beside `out_dir` and moved into place only once it is complete, so a failed build leaves an
    earlier copy as it was. `force` replaces an earlier copy; nothing else is ever removed.
    """
    from .service import Service
    from .store import utc_now

    ws = Workspace(ws_path)
    out_dir = _real(out_dir)
    _check_place(out_dir, [_real(ws.path), _real(ws.packet_dir())])
    if os.path.isdir(out_dir) and os.listdir(out_dir):
        if not is_copy(out_dir):
            raise SiteError(f"{out_dir} holds other files. Choose an empty or new folder for the copy.")
        if not force:
            raise SiteError(f"{out_dir} holds an earlier copy. Add --force to replace it.")
    elif os.path.exists(out_dir) and not os.path.isdir(out_dir):
        raise SiteError(f"{out_dir} is a file, not a folder.")
    runtime = pyodide_dir or fetch_pyodide(log=log)
    missing = [f for f in PYODIDE_FILES if not os.path.isfile(os.path.join(runtime, f))]
    if missing:
        raise SiteError(f"{runtime} is missing Pyodide files: {', '.join(missing)}")

    made_at = made_at or utc_now()[:10]
    parent = os.path.dirname(out_dir)
    os.makedirs(parent, exist_ok=True)
    stage = tempfile.mkdtemp(prefix=f".{os.path.basename(out_dir)}.building-", dir=parent)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            svc = Service(Workspace(_working_copy(ws, os.path.join(tmp, "source"))))
            snap = os.path.join(tmp, "lab")
            _snapshot(svc, snap, made_at, how_to_add)
            _check_snapshot(os.path.join(snap, "workspace"))
            _copy_web(stage)
            _copy_runtime(runtime, stage)
            _package(stage)
            data = _write_data(snap, stage)
        _write(stage, "_headers", HEADERS)
        _write(stage, "robots.txt", "User-agent: *\nDisallow: /\n")
        _write(stage, ".gitignore", "# A Lab Network copy holds a lab's notebook; never commit it.\n*\n")
        _write(stage, MARKER, json.dumps({"madeBy": "labnetwork site", "madeAt": made_at}) + "\n")
        big = [p for p in _all_files(stage) if os.path.getsize(p) > MAX_FILE]
        if big:
            raise SiteError("These files are larger than 25 MB, which Cloudflare Pages refuses: "
                            + ", ".join(os.path.relpath(p, stage) for p in big))
        files = _all_files(stage)
        total = sum(os.path.getsize(p) for p in files)
        _swap(stage, out_dir)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    log(f"Shared copy of {ws.config.get('labName') or 'the lab'} as of {made_at}: {len(files)} files, "
        f"{total / 1e6:.1f} MB, in {out_dir}")
    return {"outDir": out_dir, "madeAt": made_at, "files": len(files), "bytes": total, "data": data}


def is_copy(path: str) -> bool:
    """A folder this module wrote: its marker, and nothing at the top level that a copy does not hold."""
    if not os.path.isfile(os.path.join(path, MARKER)):
        return False
    return all(name in TOP_LEVEL or name.startswith("pyodide-") for name in os.listdir(path))


def _real(path: str) -> str:
    path = os.path.abspath(os.path.expanduser(path))
    return os.path.normcase(os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path)))


def _within(path: str, root: str) -> bool:
    if sys.platform == "darwin" or os.name == "nt":      # file names there ignore case by default
        path, root = path.lower(), root.lower()
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _check_place(out_dir: str, protected: List[str]) -> None:
    """The copy's folder must not be, hold or sit inside the workspace, the packet or this kit."""
    kit = os.path.normcase(os.path.realpath(KIT))
    if _within(out_dir, kit) or _within(kit, out_dir):
        raise SiteError("Build the copy outside the Lab Network folder, so that it never ends up in the repository.")
    for p in protected:
        p = os.path.normcase(p)
        if _within(out_dir, p) or _within(p, out_dir):
            raise SiteError(f"Put the copy in a folder of its own, away from {p}.")


def _swap(stage: str, out_dir: str) -> None:
    """Move the finished copy into place; an earlier copy is removed only after the new one is there."""
    if not os.path.exists(out_dir):
        os.replace(stage, out_dir)
        return
    if not os.listdir(out_dir):
        os.rmdir(out_dir)
        os.replace(stage, out_dir)
        return
    old = tempfile.mkdtemp(prefix=f".{os.path.basename(out_dir)}.old-", dir=os.path.dirname(out_dir))
    os.rmdir(old)
    os.replace(out_dir, old)
    os.replace(stage, out_dir)
    shutil.rmtree(old, ignore_errors=True)


def _snapshot(svc, root: str, made_at: str, how_to_add: str) -> None:
    """The notebook as it is now, as a new packet beside a workspace that holds the derived files."""
    from .packet import BUNDLED_SCHEMA, sha256_bytes

    ws = svc.ws
    pdir, wdir = os.path.join(root, "packet"), os.path.join(root, "workspace")
    os.makedirs(pdir)
    os.makedirs(wdir)
    nb = svc.nb.to_notebook()
    people = {c.get("id") for c in nb.get("contacts") or []}
    nb["edges"] = [e for e in nb.get("edges") or [] if e.get("from") in people and e.get("to") in people]
    nb["meetings"] = [m for m in nb.get("meetings") or [] if m.get("contactId") in people]
    data = json.dumps(nb, ensure_ascii=False, indent=1).encode("utf-8")
    with open(os.path.join(pdir, "notebook.json"), "wb") as f:
        f.write(data)
    for name in PACKET_FILES:
        src = os.path.join(ws.packet_dir(), name)
        if os.path.isfile(src):
            shutil.copyfile(src, os.path.join(pdir, name))
    shutil.copyfile(BUNDLED_SCHEMA, os.path.join(pdir, "notebook.schema.json"))    # see packet.bundled_schema
    for name in WORKSPACE_FILES:
        src = os.path.join(ws.path, name)
        if os.path.isfile(src):
            shutil.copyfile(src, os.path.join(wdir, name))
    digests = os.path.join(ws.path, "digests")
    if os.path.isdir(digests):
        shutil.copytree(digests, os.path.join(wdir, "digests"), ignore=shutil.ignore_patterns(".*"))
    shipped = {"contact": people, "meeting": {m.get("id") for m in nb["meetings"]},
               "edge": {e.get("id") for e in nb["edges"]}}
    kept = [e for e in svc.nb.events if _kept(e, shipped)]
    with open(os.path.join(wdir, LOG_NAME), "w", encoding="utf-8") as f:
        for seq, e in enumerate(kept, 1):
            f.write(json.dumps({**e, "seq": seq}, ensure_ascii=False) + "\n")
    settings = {k: v for k, v in (ws.config.get("settings") or {}).items() if k in SETTINGS_KEPT}
    settings.update({"sharedCopy": {"madeAt": made_at, "howToAdd": how_to_add.strip()}, "liveCheck": False})
    config = {"format": ws.config.get("format", "labnetwork-workspace"),
              "formatVersion": ws.config.get("formatVersion", 1), "workspaceId": ws.config.get("workspaceId", ""),
              "createdAt": made_at, "labName": ws.config.get("labName", ""),
              "packet": {"path": "../packet", "sha256": sha256_bytes(data), "bytes": len(data)},
              "roots": ws.config.get("roots"), "settings": settings}
    with open(os.path.join(wdir, CONFIG_NAME), "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def _kept(ev: Dict[str, Any], shipped: Dict[str, set]) -> bool:
    """Log entries a copy keeps: state the records do not hold, for records the copy has (see the docstring)."""
    op = ev.get("op", "")
    kind, _, action = op.partition(".")
    if op == "workspace.note":
        return (ev.get("data") or {}).get("kind") in NOTE_KINDS
    if action == "meta":
        return kind == "meeting" and ev.get("id") in shipped["meeting"]
    if action == "alias":
        return ev.get("id") in shipped.get(kind, ())
    return False


def _working_copy(ws: Workspace, dest: str) -> str:
    """The workspace files a build reads, copied, so that reading never touches the live change log."""
    os.makedirs(dest)
    for name in (LOG_NAME,) + WORKSPACE_FILES:
        src = os.path.join(ws.path, name)
        if os.path.isfile(src):
            shutil.copyfile(src, os.path.join(dest, name))
    if os.path.isdir(os.path.join(ws.path, "digests")):
        shutil.copytree(os.path.join(ws.path, "digests"), os.path.join(dest, "digests"))
    config = json.loads(json.dumps(ws.config))
    config["packet"]["path"] = ws.packet_dir()
    with open(os.path.join(dest, CONFIG_NAME), "w", encoding="utf-8") as f:
        json.dump(config, f)
    return dest


def _check_snapshot(wdir: str) -> None:
    """Open the snapshot as the browser will, so a copy that cannot start, or fails its checks, is never written.
    The checks go into the copy, and the browser uses them instead of running them again."""
    from .service import Service
    svc = Service(Workspace(wdir))
    failed = [c["check"] for c in svc.report["checks"] if not c["ok"]]
    if failed:
        raise SiteError("The copy's notebook fails these packet checks: " + "; ".join(failed))
    with open(os.path.join(wdir, CHECKS_NAME), "w", encoding="utf-8") as f:
        json.dump(dict(svc.report, packetPath=""), f, ensure_ascii=False)


def _copy_web(out_dir: str) -> None:
    shutil.copytree(WEB_DIR, out_dir, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".*"))
    index = os.path.join(out_dir, "index.html")
    with open(index, "r", encoding="utf-8") as f:
        html = f.read()
    if '<html lang="en">' not in html or "<head>" not in html:
        raise SiteError("web/index.html has changed shape; update site.py to match.")
    html = html.replace('<html lang="en">', '<html lang="en" data-backend="browser">', 1)
    html = html.replace("<head>", '<head>\n  <meta name="robots" content="noindex, nofollow">', 1)
    # Relative paths, so the copy also works in a subfolder of a domain.
    for ref in ('href="/img/', 'href="/css/', 'src="/js/'):
        html = html.replace(ref, ref.replace('="/', '="'))
    with open(index, "w", encoding="utf-8") as f:
        f.write(html)


def _copy_runtime(runtime: str, out_dir: str) -> None:
    dst = os.path.join(out_dir, PYODIDE_DIR)
    os.makedirs(dst, exist_ok=True)
    for name in PYODIDE_FILES:
        shutil.copyfile(os.path.join(runtime, name), os.path.join(dst, name))


def _package(out_dir: str) -> None:
    """This package as a zip for Pyodide: its Python files and the bundled schema."""
    path = os.path.join(out_dir, "py", "labnetwork.zip")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, dirnames, filenames in os.walk(HERE):
            dirnames[:] = sorted(d for d in dirnames if d != "__pycache__" and not d.startswith("."))
            for name in sorted(filenames):
                if name.endswith((".py", ".json")):
                    full = os.path.join(dirpath, name)
                    z.write(full, os.path.join("labnetwork", os.path.relpath(full, HERE)))


def _write_data(snap: str, out_dir: str) -> List[Dict[str, Any]]:
    """Each snapshot file gzipped under data/, with a manifest saying where it goes in the browser."""
    entries = []
    for full in _all_files(snap):
        rel = os.path.relpath(full, snap).replace(os.sep, "/")
        dst = os.path.join(out_dir, "data", rel + ".gz")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(full, "rb") as src, open(dst, "wb") as raw:
            with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz:
                shutil.copyfileobj(src, gz)
        entries.append({"path": f"{BROWSER_ROOT}/{rel}", "url": f"data/{rel}.gz", "bytes": os.path.getsize(full)})
    manifest = {"workspace": f"{BROWSER_ROOT}/workspace", "pyodide": PYODIDE_DIR + "/", "files": entries}
    _write(out_dir, os.path.join("data", "manifest.json"), json.dumps(manifest, indent=1))
    return entries


def _write(root: str, rel: str, text: str) -> None:
    with open(os.path.join(root, rel), "w", encoding="utf-8") as f:
        f.write(text)


def _all_files(root: str) -> List[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        out.extend(os.path.join(dirpath, n) for n in sorted(filenames))
    return out


# --------------------------------------------------------------------------- Pyodide

def runtime_dir() -> str:
    from .expand import cache_root
    return os.path.join(cache_root(), PYODIDE_DIR)


def fetch_pyodide(dest: Optional[str] = None, log: Callable[[str], None] = print,
                  opener: Callable[[str], bytes] = None) -> str:
    """The Pyodide files, downloaded once from npm and checked against the pinned checksum."""
    dest = dest or runtime_dir()
    if all(os.path.isfile(os.path.join(dest, f)) for f in PYODIDE_FILES):
        return dest
    log(f"Downloading Pyodide {PYODIDE_VERSION} (about 6 MB) from npm…")
    try:
        data = (opener or _download)(PYODIDE_URL)
    except OSError as exc:
        raise SiteError(f"Pyodide could not be downloaded from npm ({exc}). Check the connection and try again.")
    digest = base64.b64encode(hashlib.sha512(data).digest()).decode()
    if digest != PYODIDE_SHA512:
        raise SiteError("The Pyodide download does not match its published checksum, so it was not used.")
    tmp = dest + ".partial"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
        for name in PYODIDE_FILES:
            member = t.getmember(f"package/{name}")
            with t.extractfile(member) as src, open(os.path.join(tmp, name), "wb") as out:
                shutil.copyfileobj(src, out)
    shutil.rmtree(dest, ignore_errors=True)
    os.replace(tmp, dest)
    return dest


def _download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "LabNetwork site builder"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


# --------------------------------------------------------------------------- preview

def preview(folder: str, port: int = 8790, open_browser: bool = False) -> int:
    """Serve a shared copy on this computer only, with the response headers it will have online."""
    import webbrowser
    from http.server import SimpleHTTPRequestHandler

    from .server import LocalServer

    folder = os.path.abspath(os.path.expanduser(folder))
    if not os.path.isfile(os.path.join(folder, "_headers")):
        raise SiteError(f"{folder} is not a shared copy (no _headers file). Build one with: labnetwork.py site")
    with open(os.path.join(folder, "_headers"), encoding="utf-8") as f:
        rules = parse_headers(f.read())
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

    class Handler(SimpleHTTPRequestHandler):
        extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".wasm": "application/wasm",
                          ".js": "text/javascript", ".mjs": "text/javascript", ".gz": "application/gzip"}

        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=folder, **kwargs)

        def send_head(self):
            # Only requests addressed to this computer, as the app's own server does (blocks DNS rebinding).
            if self.headers.get("Host", "") not in hosts:
                self.send_error(403, "Only requests addressed to localhost are answered")
                return None
            return super().send_head()

        def end_headers(self):
            path = self.path.split("?", 1)[0]
            for pattern, headers in rules:
                if _path_matches(pattern, path):
                    for name, value in headers:
                        self.send_header(name, value)
            super().end_headers()

        def log_message(self, fmt, *args):
            pass

    try:
        server = LocalServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        raise SiteError(f"Port {port} is not free ({exc.strerror}). Choose another with --port.")
    url = f"http://127.0.0.1:{port}/"
    print(f"Shared copy at {url} (this computer only). Stop with Ctrl-C.")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def parse_headers(text: str) -> List[Any]:
    """The rules in a Cloudflare Pages _headers file: [(path pattern, [(name, value), ...]), ...]."""
    rules: List[Any] = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            rules.append((line.strip(), []))
        elif rules and ":" in line:
            name, value = line.strip().split(":", 1)
            rules[-1][1].append((name.strip(), value.strip()))
    return rules


def _path_matches(pattern: str, path: str) -> bool:
    if pattern.endswith("*"):
        return path.startswith(pattern[:-1])
    return path == pattern
