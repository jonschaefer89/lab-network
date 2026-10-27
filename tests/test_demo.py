"""The bundled fictional demo packet and research library stay valid, fictional and usable."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from labnetwork import cli, library  # noqa: E402
from labnetwork.connectors.http import Client  # noqa: E402
from labnetwork.packet import load_packet, validation_report  # noqa: E402
from labnetwork.service import Service  # noqa: E402
from labnetwork.store import Workspace  # noqa: E402

DEMO = os.path.join(ROOT, "demo", "packet")
DEMO_LIBRARY = os.path.join(ROOT, "demo", "library.json")


class DemoTests(unittest.TestCase):
    def test_demo_packet_validates(self):
        rep = validation_report(load_packet(DEMO))
        self.assertTrue(rep["ok"], [c for c in rep["checks"] if not c["ok"]])
        self.assertGreater(rep["counts"]["people"], 150)

    def test_demo_is_fictional(self):
        nb = load_packet(DEMO).notebook
        urls = [c.get("profileSourceUrl", "") for c in nb["contacts"]] + [e["sourceUrl"] for e in nb["edges"] if e["sourceUrl"]]
        self.assertTrue(all(u.startswith("https://example.org/") for u in urls if u))

    def test_demo_workspace_serves_a_map(self):
        tmp = tempfile.mkdtemp()
        try:
            svc = Service(Workspace.create(os.path.join(tmp, "ws"), DEMO, lab_name="Demo"))
            net = svc.network({})
            self.assertGreater(net["counts"]["shown"], 50)
            self.assertEqual(len(net["roots"]), 5)
            r = svc.search({"q": "ADHD"})
            self.assertGreater(r["counts"]["profiles"] + r["counts"]["papers"], 0)
        finally:
            shutil.rmtree(tmp)


class DemoFolderTests(unittest.TestCase):
    def test_the_demo_refuses_a_real_labs_workspace(self):
        from tests.fixtures import notebook, write_packet
        from labnetwork.store import WorkspaceError
        tmp = tempfile.mkdtemp()
        try:
            ws = Workspace.create(os.path.join(tmp, "real"), write_packet(tmp, notebook()), lab_name="Real Lab")
            with open(os.path.join(ws.path, "library.json"), "w") as f:
                f.write('{"people": {"pi": {}}}')
            with self.assertRaises(WorkspaceError):
                cli.demo_workspace(ws.path)
            with open(os.path.join(ws.path, "library.json")) as f:
                self.assertEqual(f.read(), '{"people": {"pi": {}}}')          # untouched
            self.assertFalse(Workspace(ws.path).config["settings"].get("demo"))
            marked = Workspace(ws.path)                 # marked as the demo by mistake: still a real lab's packet
            marked.config["settings"]["demo"] = True
            marked.save_config()
            with self.assertRaises(WorkspaceError):
                cli.demo_workspace(ws.path)
            self.assertTrue(os.path.exists(os.path.join(ws.path, "workspace.json")))   # not set aside
        finally:
            shutil.rmtree(tmp)


class DemoSymlinkTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "symbolic links need extra rights on Windows")
    def test_the_demo_reopens_its_own_folder_through_a_symlink(self):
        tmp = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(tmp, "real"))
            os.symlink(os.path.join(tmp, "real"), os.path.join(tmp, "link"))
            target = os.path.join(tmp, "link", "demo")
            with contextlib.redirect_stdout(io.StringIO()):
                cli.demo_workspace(target)
                ws = cli.demo_workspace(target)          # the second start must accept its own workspace
            self.assertTrue(ws.config["settings"]["demo"])
            self.assertFalse(os.path.exists(target + "-old"))
        finally:
            shutil.rmtree(tmp)


class DemoLibraryTests(unittest.TestCase):
    def setUp(self):
        with open(DEMO_LIBRARY, encoding="utf-8") as f:
            self.lib = json.load(f)

    def test_demo_library_is_fictional(self):
        lib = self.lib
        urls = [w["url"] for w in lib["works"].values()] + [a["url"] for a in lib["awards"].values()]
        urls += [r["openalex"]["url"] for r in lib["people"].values() if r["openalex"].get("url")]
        self.assertTrue(urls)
        self.assertTrue(all(u.startswith("https://example.org/") for u in urls))
        self.assertTrue(all(a["funder"].endswith("(fictional)") for a in lib["awards"].values()))
        bases = [r["openalex"]["basis"] for r in lib["people"].values() if r["openalex"]["status"] == "linked"]
        bases += [r["nih"]["basis"] for r in lib["people"].values() if r["nih"]["status"] == "matched"]
        self.assertTrue(all(b.endswith("(fictional)") for b in bases))
        self.assertTrue(all(w["doi"] == "" for w in lib["works"].values()))

    def test_demo_library_matches_the_demo_packet(self):
        ids = {c["id"] for c in load_packet(DEMO).notebook["contacts"]}
        self.assertLessEqual(set(self.lib["people"]), ids)
        for wid, w in self.lib["works"].items():
            self.assertTrue(all(wid in self.lib["people"][p]["openalex"]["works"] for p in w["people"]))
        keep = self.lib["standard"]["perPerson"]
        for r in self.lib["people"].values():          # each list is as long as the stated limit allows
            o = r["openalex"]
            if o["status"] == "linked":
                self.assertEqual(len(o["works"]), min(keep, o["summary"]["worksSince"]))

    def test_demo_workspace_has_the_library_and_makes_no_lookups(self):
        tmp = tempfile.mkdtemp()
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                ws = cli.demo_workspace(os.path.join(tmp, "demo"))
            self.assertTrue(ws.config["settings"]["demo"])
            svc = Service(ws)
            view = svc.person("amara-okonkwo", {"maxDegree": "all", "doctorate": "any", "identified": "0"})["library"]
            self.assertGreater(len(view["works"]), 10)
            self.assertTrue(view["openalex"]["url"].startswith("https://example.org/"))

            def no_network(*args):
                raise AssertionError("the demo reached the network")
            svc._live_client = Client(transport=no_network)
            self.assertTrue(svc.latest("amara-okonkwo")["skipped"])
            r = svc.search({"q": "ADHD", "maxDegree": "all", "doctorate": "any", "identified": "0"})
            self.assertTrue(any(a["basis"] == "openalex" for p in r["papers"] for a in p["associations"]))
            d = svc.digests()
            self.assertEqual([x["kind"] for x in d["digests"]], ["library"])
            self.assertTrue(d["library"]["demo"])
            with self.assertRaises(library.LibraryError):
                library.run(ws.path, client=Client(transport=no_network), log=lambda m: None)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["schedule", ws.path, "--every", "week"]), 1)
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
