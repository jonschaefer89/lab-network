"""HTTP API: request guards, static files, and persistence across a real restart."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests import fixtures  # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, ws_path):
        self.port = free_port()
        self.err = tempfile.TemporaryFile()
        self.proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "labnetwork.py"), "serve", ws_path,
                                      "--port", str(self.port)], stdout=subprocess.DEVNULL, stderr=self.err)
        deadline = time.time() + 60           # hosted test machines can be slow to start Python
        while time.time() < deadline and self.proc.poll() is None:
            try:
                self.get("/api/state")
                return
            except (urllib.error.URLError, ConnectionError):
                time.sleep(0.1)
        self.stop()
        self.err.seek(0)
        raise RuntimeError("server did not start: " + self.err.read().decode("utf-8", "replace")[-2000:])

    def request(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        h = {"Content-Type": "application/json"} if body is not None else {}
        h.update(headers or {})
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read(), dict(e.headers)

    def get(self, path, **kw):
        status, body, _ = self.request("GET", path, **kw)
        return status, json.loads(body) if body[:1] in (b"{", b"[") else body

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.ws = fixtures.new_workspace()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_guards_and_persistence(self):
        srv = Server(self.ws.path)
        try:
            status, state = srv.get("/api/state")
            self.assertEqual(status, 200)
            self.assertEqual([r["id"] for r in state["lab"]["roots"]], ["pi", "stu", "pd"])
            status, _, headers = srv.request("GET", "/")
            self.assertEqual(status, 200)
            self.assertIn("default-src 'self'", headers.get("Content-Security-Policy", ""))
            # DNS-rebinding guard
            self.assertEqual(srv.request("GET", "/api/state", headers={"Host": "evil.example"})[0], 403)
            # Writes must be JSON from this origin
            req = urllib.request.Request(f"http://127.0.0.1:{srv.port}/api/notes", data=b"a=b", method="POST",
                                         headers={"Content-Type": "application/x-www-form-urlencoded"})
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(cm.exception.code, 415)
            cm.exception.close()
            status, _, _ = srv.request("POST", "/api/notes", {"fields": {}}, {"Origin": "https://evil.example"})
            self.assertEqual(status, 403)
            # Path traversal stays inside web/
            self.assertEqual(srv.request("GET", "/../labnetwork/service.py")[0], 404)
            # A real write
            status, body, _ = srv.request("POST", "/api/notes", {
                "fields": {"contactId": "c1", "metBy": "Pat Investigator", "date": "2026-10-12",
                           "context": "Restart test note (temporary)", "learning": "", "followUp": ""}},
                {"X-LabNetwork-Actor": "Pat Investigator"})
            self.assertEqual(status, 200, body)
            note_id = json.loads(body)["id"]
            self.assertEqual(srv.request("POST", "/api/notes", {"fields": {"contactId": "c1"}})[0], 400)
            # A second server on the same workspace is refused while the first runs.
            port2 = free_port()
            second = subprocess.run([sys.executable, os.path.join(ROOT, "labnetwork.py"), "serve", self.ws.path,
                                     "--port", str(port2)], capture_output=True, text=True, timeout=15)
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("Another Lab Network server", second.stdout + second.stderr)
        finally:
            srv.stop()
        srv = Server(self.ws.path)
        try:
            status, notes = srv.get("/api/notes")
            ids = [n["id"] for n in notes["notes"]]
            self.assertIn(note_id, ids)
            mine = [n for n in notes["notes"] if n["id"] == note_id][0]
            self.assertEqual(mine["recordedBy"], "Pat Investigator")
            status, _, headers = srv.request("GET", "/api/export?format=bundle")
            self.assertEqual(status, 200)
            self.assertIn("attachment", headers.get("Content-Disposition", ""))
        finally:
            srv.stop()


    def test_marking_a_record_not_theirs_through_the_api_and_the_command_line(self):
        srv = Server(self.ws.path)
        try:
            path = "/api/contacts/pi/not-theirs"
            status, body, _ = srv.request("POST", path, {"openalexId": "A5123", "note": "A namesake"},
                                          {"X-LabNetwork-Actor": "Pat%20Investigator"})
            self.assertEqual((status, json.loads(body)["changed"]), (200, True))
            self.assertEqual(srv.request("POST", path, {"openalexId": "W5123"})[0], 400)     # not an author record
            self.assertEqual(srv.request("POST", "/api/contacts/nobody/not-theirs", {"openalexId": "A5123"})[0], 404)
            status, person = srv.get("/api/person/pi")
            self.assertEqual([(m["id"], m["actor"], m["url"]) for m in person["openalexNotTheirs"]],
                             [("A5123", "Pat Investigator", "https://openalex.org/A5123")])
            # The command line sends its change through the running app, under the name it is given.
            run = subprocess.run([sys.executable, os.path.join(ROOT, "labnetwork.py"), "not-theirs", self.ws.path, "pi",
                                  "A5123", "--undo", "--actor", "Sam Student"], capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("Withdrew the mark on A5123.", run.stdout)
            status, person = srv.get("/api/person/pi")
            self.assertEqual(person["openalexNotTheirs"], [])
            status, history = srv.get("/api/history")
            notes = sorted((e for e in history["events"] if e["op"] == "workspace.note"), key=lambda e: e["seq"])
            self.assertEqual([e["actor"] for e in notes], ["Pat Investigator", "Sam Student"])
        finally:
            srv.stop()


if __name__ == "__main__":
    unittest.main()
