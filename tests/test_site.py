"""A lab's shared read-only copy: what goes into it, what stays out, where it may be written, and how its in-browser
backend answers."""
import base64
import contextlib
import gzip
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import unittest
import zipfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from labnetwork import cli, site  # noqa: E402
from labnetwork.browser import Backend  # noqa: E402
from labnetwork.service import Service  # noqa: E402
from labnetwork.store import Workspace  # noqa: E402
from tests.fixtures import write_packet  # noqa: E402


def fake_runtime(directory):
    """Stand-ins for the Pyodide files: the builder only copies them."""
    os.makedirs(directory, exist_ok=True)
    for name in site.PYODIDE_FILES:
        with open(os.path.join(directory, name), "w") as f:
            f.write(name)
    return directory


class SiteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        lab = os.path.join(self.tmp, "lab")
        packet = write_packet(lab)
        with open(os.path.join(packet, "READ-ME-FIRST.txt"), "w") as f:
            f.write("the packet's own brief")
        # Onboard lists files in the checksums that a copy leaves out; the copy must still pass its checks.
        with open(os.path.join(packet, "build-sources.json"), "w") as f:
            f.write("{}")
        with open(os.path.join(packet, "packet-checksums.json"), "w") as f:
            json.dump({"build-sources.json": hashlib.sha256(b"{}").hexdigest()}, f)
        self.ws_path = os.path.join(lab, "workspace")
        ws = Workspace.create(self.ws_path, packet, lab_name="Test Lab")
        ws.config["settings"]["contactEmail"] = "pi@example.org"
        ws.save_config()
        svc = Service(ws)
        # Things a shared copy must leave out: a note archived later, a value changed later, Slack submissions,
        # imported files.
        note = svc.create_note({"fields": {"contactId": "c1", "metBy": "Pat Investigator", "date": "2026-01-02",
                                           "context": "Coffee", "learning": "an archived private remark"}}, "Pat")
        svc.archive("meetings", note["id"], "Pat")
        svc.update_contact("c1", {"set": {"role": "Professor of Something Newer"}}, "Pat")
        # State that only the change log holds, which a copy must keep: a follow-up marked done, and an OpenAlex
        # record marked as not someone's.
        kept = svc.create_note({"fields": {"contactId": "c1", "metBy": "Pat Investigator", "date": "2026-02-03",
                                           "context": "Conference", "followUp": "Send the preprint"}}, "Pat")
        svc.update_note(kept["id"], {"set": {"followUpStatus": "done"}}, "Pat")
        self.kept_note = kept["id"]
        svc.mark_not_theirs("c1", {"openalexId": "A5000000001", "note": "a different researcher"}, "Pat")
        with open(os.path.join(self.ws_path, "intake.json"), "w") as f:
            json.dump({"items": {"1.1": {"text": "a private Slack submission"}}}, f)
        os.makedirs(os.path.join(self.ws_path, "imports"))
        with open(os.path.join(self.ws_path, "imports", "b1--contacts.csv"), "w") as f:
            f.write("name\nSomeone Imported\n")
        os.makedirs(os.path.join(self.ws_path, "digests"))
        with open(os.path.join(self.ws_path, "digests", "library-2026-10-01.md"), "w") as f:
            f.write("# Library update\n")
        self.runtime = fake_runtime(os.path.join(self.tmp, "runtime"))
        self.out = os.path.join(self.tmp, "site")

    def build(self, **kw):
        return site.build(self.ws_path, self.out, how_to_add="Post additions in #lab-network.",
                          pyodide_dir=self.runtime, made_at="2026-10-27", log=lambda m: None, **kw)

    def manifest(self):
        with open(os.path.join(self.out, "data", "manifest.json")) as f:
            return json.load(f)

    def unpack(self, entry):
        with gzip.open(os.path.join(self.out, entry["url"]), "rb") as f:
            return f.read()

    def entries(self):
        return {e["path"]: e for e in self.manifest()["files"]}

    def test_copy_holds_the_app_runtime_package_and_data(self):
        self.build()
        with open(os.path.join(self.out, "index.html")) as f:
            html = f.read()
        self.assertIn('data-backend="browser"', html)
        self.assertIn('content="noindex, nofollow"', html)
        self.assertIn('src="js/main.js"', html)                  # relative, so a subfolder works too
        for name in site.PYODIDE_FILES:
            self.assertTrue(os.path.isfile(os.path.join(self.out, site.PYODIDE_DIR, name)))
        self.assertEqual(self.manifest()["pyodide"], site.PYODIDE_DIR + "/")
        self.assertTrue(os.path.isfile(os.path.join(self.out, "js", "site-worker.js")))
        with zipfile.ZipFile(os.path.join(self.out, "py", "labnetwork.zip")) as z:
            names = set(z.namelist())
        self.assertIn("labnetwork/browser.py", names)
        self.assertIn("labnetwork/schema/notebook.schema.json", names)
        self.assertFalse(any("__pycache__" in n for n in names))
        with open(os.path.join(self.out, "_headers")) as f:
            rules = dict(site.parse_headers(f.read()))
        csp = dict(rules["/*"])["Content-Security-Policy"]
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("'wasm-unsafe-eval'", csp)
        self.assertNotIn("'unsafe-eval'", csp)
        self.assertIn(f"/{site.PYODIDE_DIR}/*", rules)
        with open(os.path.join(self.out, "robots.txt")) as f:
            self.assertIn("Disallow: /", f.read())
        with open(os.path.join(self.out, ".gitignore")) as f:
            self.assertIn("*", f.read().split())                 # never committed, wherever it is built
        self.assertTrue(site.is_copy(self.out))

    def test_only_the_current_notebook_goes_into_the_copy(self):
        self.build()
        paths = set(self.entries())
        self.assertIn("/lab/workspace/workspace.json", paths)
        self.assertIn("/lab/packet/notebook.json", paths)
        self.assertIn("/lab/workspace/digests/library-2026-10-01.md", paths)
        for left_out in ("intake.json", "imports", "READ-ME-FIRST.txt", "build-sources.json", "packet-checksums.json",
                         "logs", ".labnetwork.lock"):
            self.assertFalse(any(left_out in p for p in paths), left_out)
        everything = b"".join(self.unpack(e) for e in self.manifest()["files"])
        for secret in (b"private Slack submission", b"Someone Imported", b"an archived private remark",
                       b"pi@example.org"):
            self.assertNotIn(secret, everything, secret)
        nb = json.loads(self.unpack(self.entries()["/lab/packet/notebook.json"]))
        c1 = [c for c in nb["contacts"] if c["id"] == "c1"][0]
        self.assertEqual(c1["role"], "Professor of Something Newer")              # current values only
        log = [json.loads(line) for line in self.unpack(self.entries()["/lab/workspace/changes.jsonl"]).splitlines()]
        self.assertEqual(sorted(e["op"] for e in log), ["meeting.meta", "workspace.note"])     # no history

    def test_snapshot_is_marked_as_a_shared_copy_and_passes_its_checks(self):
        self.build()
        config = json.loads(self.unpack(self.entries()["/lab/workspace/workspace.json"]))
        self.assertEqual(config["settings"]["sharedCopy"],
                         {"madeAt": "2026-10-27", "howToAdd": "Post additions in #lab-network."})
        self.assertIs(config["settings"]["liveCheck"], False)
        self.assertNotIn("contactEmail", config["settings"])
        self.assertEqual(config["packet"]["path"], "../packet")
        # The workspace itself is not touched.
        with open(os.path.join(self.ws_path, "workspace.json")) as f:
            self.assertNotIn("sharedCopy", json.load(f).get("settings", {}))

    def unpacked_backend(self):
        lab = os.path.join(self.tmp, "unpacked")
        for e in self.manifest()["files"]:
            dst = os.path.join(lab, e["path"].lstrip("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as f:
                f.write(self.unpack(e))
        return Backend(os.path.join(lab, "lab", "workspace"))

    def test_backend_answers_reads_and_refuses_writes(self):
        self.build()
        backend = self.unpacked_backend()
        state = json.loads(backend.handle("GET", "/api/state"))
        self.assertEqual(state["status"], 200)
        self.assertEqual(state["body"]["sharedCopy"]["madeAt"], "2026-10-27")
        self.assertTrue(state["body"]["packet"]["ok"], state["body"]["packet"]["checks"])
        self.assertEqual(json.loads(backend.handle("GET", "/api/search", json.dumps({"q": "ADHD"})))["status"], 200)
        live = json.loads(backend.handle("GET", "/api/person/pi/latest"))
        self.assertTrue(live["body"]["skipped"])
        self.assertIn("shared copy", live["body"]["notes"][0])
        digests = json.loads(backend.handle("GET", "/api/digests"))["body"]["library"]
        self.assertTrue(digests["sharedCopy"])
        self.assertEqual(digests["commands"], {})
        notes = json.loads(backend.handle("GET", "/api/notes"))["body"]
        done = [n for n in notes["notes"] if n["id"] == self.kept_note][0]
        self.assertEqual(done["followUpStatus"], "done")
        person = json.loads(backend.handle("GET", "/api/person/c1"))["body"]
        self.assertEqual([m["url"].rsplit("/", 1)[-1] for m in person["openalexNotTheirs"]], ["A5000000001"])
        self.assertEqual(json.loads(backend.handle("POST", "/api/notes", "{}"))["status"], 403)
        self.assertEqual(json.loads(backend.handle("GET", "/api/person/nobody"))["status"], 404)

    def test_force_replaces_only_an_earlier_copy(self):
        os.makedirs(self.out)
        self.build()                                    # an empty folder is fine
        with self.assertRaises(site.SiteError):
            self.build()                                # an earlier copy needs --force
        with open(os.path.join(self.out, ".DS_Store"), "w") as f:      # Finder opened the folder
            f.write("x")
        self.build(force=True)
        # A failed build leaves the earlier copy as it was.
        with mock.patch.object(site, "_check_snapshot", side_effect=site.SiteError("broken")):
            with self.assertRaises(site.SiteError):
                self.build(force=True)
        self.assertTrue(site.is_copy(self.out))
        self.assertEqual(sorted(n for n in os.listdir(self.tmp) if ".building-" in n or ".old-" in n), [])
        # A folder that merely looks like a site is never removed.
        other = os.path.join(self.tmp, "other-site")
        os.makedirs(os.path.join(other, ".git"))
        for name in ("_headers", site.MARKER):
            with open(os.path.join(other, name), "w") as f:
                f.write("x")
        with self.assertRaises(site.SiteError):
            site.build(self.ws_path, other, pyodide_dir=self.runtime, force=True, log=lambda m: None)
        self.assertTrue(os.path.isdir(os.path.join(other, ".git")))

    def test_refuses_to_build_inside_the_workspace_the_packet_or_the_kit(self):
        for place in (os.path.join(self.ws_path, "site"), os.path.dirname(self.ws_path),
                      os.path.join(self.tmp, "lab", "packet", "site"), os.path.join(site.KIT, "my-site")):
            with self.assertRaises(site.SiteError, msg=place):
                site.build(self.ws_path, place, pyodide_dir=self.runtime, force=True, log=lambda m: None)
        self.assertTrue(os.path.isfile(os.path.join(self.ws_path, "workspace.json")))
        self.assertFalse(os.path.exists(os.path.join(site.KIT, "my-site")))
        if sys.platform == "darwin" or os.name == "nt":          # names that differ only in case are the same folder
            with self.assertRaises(site.SiteError):
                site.build(self.ws_path, os.path.join(self.ws_path.upper(), "site"), pyodide_dir=self.runtime,
                           log=lambda m: None)

    def test_demo_copy_comes_from_a_fresh_demo(self):
        out = os.path.join(self.tmp, "demo-site")
        with mock.patch.object(site, "fetch_pyodide", return_value=self.runtime), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["site", "--demo", "--out", out]), 0)
        self.assertTrue(site.is_copy(out))
        with open(os.path.join(out, "data", "manifest.json")) as f:
            paths = [e["path"] for e in json.load(f)["files"]]
        self.assertIn("/lab/workspace/library.json", paths)

    def test_pyodide_download_is_checked_against_its_checksum(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            for name in site.PYODIDE_FILES:
                data = name.encode()
                info = tarfile.TarInfo(f"package/{name}")
                info.size = len(data)
                t.addfile(info, io.BytesIO(data))
        tampered = buf.getvalue()
        with self.assertRaises(site.SiteError):
            site.fetch_pyodide(os.path.join(self.tmp, "py-a"), log=lambda m: None, opener=lambda url: tampered)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "py-a")))
        good = base64.b64encode(hashlib.sha512(tampered).digest()).decode()
        with mock.patch.object(site, "PYODIDE_SHA512", good):
            dest = site.fetch_pyodide(os.path.join(self.tmp, "py-b"), log=lambda m: None, opener=lambda url: tampered)
        self.assertEqual(sorted(os.listdir(dest)), sorted(site.PYODIDE_FILES))

    def test_headers_file_parses(self):
        rules = site.parse_headers(site.HEADERS)
        self.assertEqual([p for p, _ in rules], ["/*", "/data/*", f"/{site.PYODIDE_DIR}/*"])
        self.assertTrue(site._path_matches(f"/{site.PYODIDE_DIR}/*", f"/{site.PYODIDE_DIR}/pyodide.asm.wasm"))
        self.assertFalse(site._path_matches("/data/*", "/js/api.js"))


if __name__ == "__main__":
    unittest.main()
