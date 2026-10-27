"""The app's JSON API inside a web browser, for a lab's shared read-only copy (built by site.py).

A shared copy is a folder of static files: the web app, this package and a snapshot of the lab's workspace. Pyodide
runs the package in the browser (web/js/site-worker.js), and each request from the web app is answered here by the
same routes the local server uses. Only reads are answered; the copy is rebuilt from the lab's own workspace.
"""
from __future__ import annotations

import json
from typing import Any, Dict

from .importer import ImportError_
from .server import App
from .service import Service, ServiceError
from .store import Workspace

READ_ONLY = ("This is the lab's read-only copy of Lab Network. Changes are made in the lab's own copy, which "
             "updates this one.")


class Backend:
    def __init__(self, workspace_path: str):
        self.svc = Service(Workspace(workspace_path))
        self.app = App(self.svc, 0)

    def handle(self, method: str, path: str, query_json: str = "{}") -> str:
        """One request, answered as JSON text: {"status": HTTP status, "body": the server's reply}."""
        status, body = self._answer(method, path, query_json)
        return json.dumps({"status": status, "body": body}, ensure_ascii=False)

    def _answer(self, method: str, path: str, query_json: str):
        if method != "GET":
            return 403, {"error": READ_ONLY}
        try:
            query: Dict[str, Any] = {str(k): str(v) for k, v in (json.loads(query_json or "{}") or {}).items()}
            return 200, self.app.dispatch(method, path, query, None, "")
        except (ServiceError, ImportError_) as exc:
            return getattr(exc, "status", 400), {"error": exc.message, "details": exc.details}
        except Exception as exc:                      # the same reply the local server gives
            return 500, {"error": f"Internal error: {type(exc).__name__}: {exc}"}
