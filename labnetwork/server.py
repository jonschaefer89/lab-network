"""Local HTTP server: JSON API plus the static web app. Binds to 127.0.0.1 only.

Requests are refused unless the Host header names this machine (blocks DNS
rebinding). Writes must be JSON from this origin, which a third-party web page
in the same browser cannot send without a CORS preflight that this server
never approves.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import socketserver
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, unquote, urlsplit

from . import exporter
from .importer import Importer, ImportError_
from .service import EditConflict, NotFound, Service, ServiceError

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
MAX_BODY = 30 * 1024 * 1024

CSP = ("default-src 'self'; img-src 'self' https: http: data:; script-src 'self'; style-src 'self'; "
       "connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

Route = Tuple[str, "re.Pattern[str]", Callable]


class LocalServer(ThreadingHTTPServer):
    """Skips HTTPServer's reverse DNS lookup of its own address, which can delay the start by many seconds on some
    Macs; the server only ever answers on 127.0.0.1."""

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class App:
    def __init__(self, svc: Service, port: int):
        self.svc = svc
        self.port = port
        self.importer = Importer(svc)
        self.routes: List[Route] = []
        r = self.route
        r("GET", r"/api/state", lambda q, b, a: svc.state())
        r("GET", r"/api/network", lambda q, b, a: svc.network(q))
        r("GET", r"/api/people", lambda q, b, a: svc.people(q))
        r("GET", r"/api/person/(?P<pid>[^/]+)", lambda q, b, a, pid: svc.person(pid, q))
        r("GET", r"/api/person/(?P<pid>[^/]+)/neighborhood", lambda q, b, a, pid: svc.neighborhood(pid, q))
        r("GET", r"/api/person/(?P<pid>[^/]+)/latest", lambda q, b, a, pid: svc.latest(pid))
        r("GET", r"/api/path", lambda q, b, a: svc.path(q))
        r("GET", r"/api/outside", lambda q, b, a: svc.outside_find(q))
        r("GET", r"/api/conferences", lambda q, b, a: svc.conference_list(q))
        r("POST", r"/api/conferences/series", lambda q, b, a: svc.conference_series(b, a))
        r("POST", r"/api/conferences/series/(?P<sid>[a-z0-9-]+)/remove",
          lambda q, b, a, sid: svc.conference_series({}, a, remove=sid))
        r("GET", r"/api/outside/(?P<aid>[Aa]\d+)", lambda q, b, a, aid: svc.outside_connect(aid, q))
        r("GET", r"/api/search", lambda q, b, a: svc.search(q))
        r("GET", r"/api/notes", lambda q, b, a: svc.notes(q))
        r("GET", r"/api/affiliations", lambda q, b, a: svc.affiliations(q))
        r("GET", r"/api/conflicts", lambda q, b, a: svc.conflicts(q))
        r("GET", r"/api/history", lambda q, b, a: svc.history(q))
        r("GET", r"/api/validation", lambda q, b, a: svc.validation())
        r("GET", r"/api/digests", lambda q, b, a: svc.digests())
        r("POST", r"/api/contacts", lambda q, b, a: svc.create_contact(b, a))
        r("PATCH", r"/api/contacts/(?P<pid>[^/]+)", lambda q, b, a, pid: svc.update_contact(pid, b, a))
        r("POST", r"/api/contacts/(?P<pid>[^/]+)/sources", lambda q, b, a, pid: svc.add_source(pid, b, a))
        r("POST", r"/api/contacts/(?P<pid>[^/]+)/not-theirs", lambda q, b, a, pid: svc.mark_not_theirs(pid, b, a))
        r("POST", r"/api/notes", lambda q, b, a: svc.create_note(b, a))
        r("PATCH", r"/api/notes/(?P<mid>[^/]+)", lambda q, b, a, mid: svc.update_note(mid, b, a))
        r("POST", r"/api/ties", lambda q, b, a: svc.create_tie(b, a))
        r("PATCH", r"/api/ties/(?P<eid>[^/]+)", lambda q, b, a, eid: svc.update_tie(eid, b, a))
        r("POST", r"/api/archive", lambda q, b, a: svc.archive(b.get("collection", ""), b.get("id", ""), a,
                                                                 restore=bool(b.get("restore"))))
        r("POST", r"/api/conflicts/(?P<cid>[^/]+)/resolve", lambda q, b, a, cid: svc.resolve_conflict(cid, b, a))
        r("POST", r"/api/workspace-notes", lambda q, b, a: svc.add_workspace_note(b, a))
        r("GET", r"/api/intake", lambda q, b, a: svc.intake())
        r("POST", r"/api/intake/check", lambda q, b, a: svc.intake_check())
        r("POST", r"/api/intake/settings", lambda q, b, a: svc.intake_settings(b))
        r("POST", r"/api/intake/items/(?P<ts>[0-9.]+)", lambda q, b, a, ts: svc.intake_decide(ts, b, a))
        r("POST", r"/api/import/undo", lambda q, b, a: svc.undo_import(b.get("batchId", ""), a))
        r("POST", r"/api/import/preview", self.import_preview)
        r("POST", r"/api/import/apply", lambda q, b, a: self.locked(
            lambda: self.importer.apply(b.get("batchId", ""), b.get("decisions") or {}, a)))

    def route(self, method: str, pattern: str, fn: Callable) -> None:
        self.routes.append((method, re.compile("^" + pattern + "$"), fn))

    def locked(self, fn):
        with self.svc.lock:
            return fn()

    def import_preview(self, q, body, actor):
        name = body.get("filename") or "upload"
        if "contentBase64" in body:
            try:
                data = base64.b64decode(body["contentBase64"], validate=True)
            except ValueError:
                raise ServiceError("contentBase64 is not valid base64")
        else:
            data = (body.get("content") or "").encode("utf-8")
        with self.svc.lock:
            return self.importer.preview(name, data)

    def dispatch(self, method: str, path: str, query: Dict[str, str], body: Any, actor: str):
        for m, rx, fn in self.routes:
            if m != method:
                continue
            match = rx.match(path)
            if match:
                kwargs = {k: unquote(v) for k, v in match.groupdict().items()}
                return fn(query, body, actor, **kwargs)
        raise NotFound(f"No API route for {method} {path}")


def make_handler(app: App):
    allowed_hosts = {f"127.0.0.1:{app.port}", f"localhost:{app.port}", f"[::1]:{app.port}"}
    allowed_origins = {f"http://{h}" for h in allowed_hosts}

    class Handler(BaseHTTPRequestHandler):
        server_version = "LabNetwork"
        sys_version = ""

        def log_message(self, fmt, *args):
            if os.environ.get("LABNETWORK_LOG") or os.environ.get("FIELDNOTES_LOG"):
                sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def _send(self, status: int, body: bytes, ctype: str, extra: Optional[Dict[str, str]] = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, obj: Any):
            self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _guard(self) -> bool:
            host = (self.headers.get("Host") or "").lower()
            if host not in allowed_hosts:
                self._json(403, {"error": "Lab Network only answers requests addressed to localhost."})
                return False
            if self.command not in ("GET", "HEAD"):
                origin = self.headers.get("Origin")
                if origin and origin not in allowed_origins:
                    self._json(403, {"error": "Cross-origin writes are not allowed."})
                    return False
                if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                    self._json(415, {"error": "Send writes as application/json."})
                    return False
            return True

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if not self._guard():
                return
            parts = urlsplit(self.path)
            if parts.path.startswith("/api/"):
                if parts.path == "/api/export":
                    return self._export(dict(parse_qsl(parts.query)))
                if parts.path.startswith("/api/templates/"):
                    return self._template(parts.path.rsplit("/", 1)[-1])
                return self._api("GET", parts)
            return self._static(parts.path)

        def do_POST(self):
            self._write("POST")

        def do_PATCH(self):
            self._write("PATCH")

        def _write(self, method):
            if not self._guard():
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._json(413, {"error": "Request body too large"})
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                return self._json(400, {"error": "Body is not valid JSON"})
            if not isinstance(body, dict):
                return self._json(400, {"error": "Body must be a JSON object"})
            self._api(method, urlsplit(self.path), body)

        def _api(self, method, parts, body=None):
            query = dict(parse_qsl(parts.query, keep_blank_values=True))
            # X-Fieldnotes-Actor is the header's earlier name; older scripts still send it.
            header = self.headers.get("X-LabNetwork-Actor") or self.headers.get("X-Fieldnotes-Actor") or ""
            actor = (unquote(header) or (body or {}).get("actor") or "").strip()[:200]
            try:
                result = app.dispatch(method, parts.path, query, body or {}, actor)
                self._json(200, result)
            except (ServiceError, ImportError_) as exc:
                status = getattr(exc, "status", 400)
                self._json(status, {"error": exc.message, "details": exc.details})
            except Exception as exc:  # noqa: BLE001 - report, never crash the server
                traceback.print_exc()
                self._json(500, {"error": f"Internal error: {type(exc).__name__}: {exc}"})

        def _export(self, query):
            fmt = query.get("format", "bundle")
            try:
                fname, ctype, data = exporter.export(app.svc, fmt)
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
            if query.get("save") == "1":
                os.makedirs(app.svc.ws.exports_dir, exist_ok=True)
                with open(os.path.join(app.svc.ws.exports_dir, fname), "wb") as f:
                    f.write(data)
            self._send(200, data, ctype + ("; charset=utf-8" if ctype.startswith("text") else ""),
                       {"Content-Disposition": f'attachment; filename="{fname}"'})

        def _template(self, name):
            try:
                data = exporter.template(name)
            except KeyError:
                return self._json(404, {"error": "No such template"})
            self._send(200, data, "text/csv; charset=utf-8", {"Content-Disposition": f'attachment; filename="{name}"'})

        def _static(self, path):
            if path in ("", "/"):
                path = "/index.html"
            rel = os.path.normpath(unquote(path).lstrip("/"))
            full = os.path.join(WEB_DIR, rel)
            if rel.startswith("..") or not os.path.realpath(full).startswith(os.path.realpath(WEB_DIR) + os.sep):
                return self._json(404, {"error": "Not found"})
            if not os.path.isfile(full):
                # Client-side routes fall back to the app shell.
                if "." not in os.path.basename(rel):
                    full = os.path.join(WEB_DIR, "index.html")
                else:
                    return self._json(404, {"error": "Not found"})
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if full.endswith(".js"):
                ctype = "text/javascript"
            with open(full, "rb") as f:
                data = f.read()
            self._send(200, data, ctype + ("; charset=utf-8" if ctype.startswith("text") else ""))

    return Handler


def _poll_slack(svc: Service, first_delay: float = 5.0) -> None:
    """While the app runs, read the Slack intake channel shortly after start and then every few minutes.

    Only outgoing requests to Slack: nothing listens for Slack, and nothing outside this computer can reach the app.
    """
    import time
    from . import intake
    time.sleep(first_delay)
    while True:
        minutes = 10
        try:
            data = intake.load(svc.ws)
            minutes = max(2, int(data.get("pollMinutes") or 10))
            if intake.configured(data) and intake.token(data):
                svc.intake_check()
        except Exception:                     # kept in intake.json where possible and shown on the Data page
            pass
        time.sleep(minutes * 60)


def serve(svc: Service, port: int = 8765, open_browser: bool = False,
          ready: Optional[threading.Event] = None) -> None:
    app = App(svc, port)
    httpd = LocalServer(("127.0.0.1", port), make_handler(app))
    httpd.daemon_threads = True
    svc.ws.acquire_lock(port)
    url = f"http://127.0.0.1:{port}/"
    print(f"Lab Network is running at {url}  (local only; press Ctrl+C to stop)", flush=True)
    if svc.nb.replay_errors:
        print(f"WARNING: {len(svc.nb.replay_errors)} logged change(s) could not be applied; see Data > Health.",
              flush=True)
    if open_browser:
        import webbrowser
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    threading.Thread(target=_poll_slack, args=(svc,), daemon=True, name="slack-intake").start()
    if ready is not None:
        ready.set()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Lab Network.")
    finally:
        httpd.server_close()
        svc.ws.release_lock()
