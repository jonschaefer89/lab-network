"""Edits, persistence, imports, exports and conflict handling."""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from labnetwork import cli, exporter  # noqa: E402
from labnetwork.importer import Importer, ImportError_  # noqa: E402
from labnetwork.packet import sha256_file  # noqa: E402
from labnetwork.service import EditConflict, Service, ServiceError  # noqa: E402
from labnetwork.store import Workspace  # noqa: E402
from tests import fixtures  # noqa: E402


def fresh():
    tmp, ws = fixtures.new_workspace()
    return tmp, Service(ws)


def reopen(svc):
    return Service(Workspace(svc.ws.path))


def snapshot(svc):
    nb = svc.nb.to_notebook(include_archived=True)
    return json.dumps(nb, sort_keys=True), sorted(nb.keys())


class EditTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.svc = fresh()
        self.packet_sha = sha256_file(os.path.join(self.svc.packet.path, "notebook.json"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_create_and_edit_contact_persists(self):
        res = self.svc.create_contact({"fields": {"name": "Robin Newcontact", "institution": "Hill College",
                                                  "role": "Professor"}}, "Pat Investigator")
        rid = res["id"]
        self.assertEqual(rid, "robin-newcontact")
        self.svc.update_contact(rid, {"set": {"role": "Associate Dean"}, "expected": {"role": "Professor"}}, "Pat")
        again = reopen(self.svc)
        self.assertEqual(again.nb.contact(rid)["role"], "Associate Dean")
        hist = again.person(rid, {})["history"]
        self.assertEqual([h["op"] for h in hist], ["contact.create", "contact.update"])
        self.assertEqual(hist[1]["changes"][0], {"field": "role", "from": "Professor", "to": "Associate Dean"})
        self.assertEqual(sha256_file(os.path.join(self.svc.packet.path, "notebook.json")), self.packet_sha)

    def test_edit_packet_contact_keeps_original(self):
        self.svc.update_contact("c1", {"set": {"institution": "Moved University"}}, "Pat")
        p = self.svc.person("c1", {})
        self.assertEqual(p["record"]["institution"], "Moved University")
        self.assertEqual(p["packetValues"], {"institution": "Lakeside University"})
        self.assertEqual(self.svc.packet.notebook["contacts"][5]["institution"], "Lakeside University")

    def test_validation_and_stale_edits(self):
        with self.assertRaises(ServiceError):
            self.svc.update_contact("c1", {"set": {"website": "not a url"}}, "Pat")
        with self.assertRaises(ServiceError):
            self.svc.update_contact("c1", {"set": {"doctorateStatus": "confirmed", "doctorateEvidence": ""}}, "Pat")
        with self.assertRaises(EditConflict):
            self.svc.update_contact("c1", {"set": {"role": "Dean"}, "expected": {"role": "Someone else's value"}}, "P")
        with self.assertRaises(ServiceError):
            self.svc.update_contact("c1", {"set": {"networkDegree": 1}}, "Pat")
        with self.assertRaises(EditConflict):  # same name as an existing person
            self.svc.create_contact({"fields": {"name": "Chris Coauthor"}}, "Pat")
        res = self.svc.create_contact({"fields": {"name": "Chris Coauthor"}, "confirmNotDuplicate": True}, "Pat")
        self.assertEqual(res["id"], "chris-coauthor")
        self.assertNotIn("chris-coauthor", self.svc.nb.base_ids["contacts"])

    def test_notes_and_firsthand_tie(self):
        res = self.svc.create_note({"fields": {"contactId": "g1", "metBy": "Pat Investigator", "date": "2026-10-10",
                                               "context": "SRP poster session", "learning": "New cohort",
                                               "followUp": "Email about data access",
                                               "recordedBy": "Pat Investigator"},
                                    "tie": {"from": "pi"}}, "Pat Investigator")
        tie = self.svc.nb.records["edges"][res["tie"]]
        self.assertEqual((tie["type"], tie["provenance"], tie["from"], tie["to"]), ("met", "firsthand", "pi", "g1"))
        res2 = self.svc.create_note({"fields": {"contactId": "g1", "metBy": "Sam Student", "context": "Hallway chat",
                                                "recordedBy": "Pat Investigator"}, "tie": {"from": "stu"}}, "Pat")
        self.assertEqual(self.svc.nb.records["edges"][res2["tie"]]["provenance"], "reported")
        self.svc.update_note(res["id"], {"set": {"followUp": "Sent the email", "followUpStatus": "done"}}, "Pat")
        again = reopen(self.svc)
        note = [m for m in again.person("g1", {})["meetings"] if m["id"] == res["id"]][0]
        self.assertEqual((note["followUp"], note["followUpStatus"]), ("Sent the email", "done"))
        with self.assertRaises(ServiceError):
            self.svc.create_note({"fields": {"contactId": "g1", "metBy": "Pat", "context": "x", "date": "2026-13-01"}},
                                 "Pat")
        orig = again.nb.records["meetings"]["meet-1"]
        self.assertEqual(orig["recordedBy"], "Pat Investigator")

    def test_archive_rules(self):
        with self.assertRaises(ServiceError):
            self.svc.archive("contacts", "c1", "Pat")
        rid = self.svc.create_contact({"fields": {"name": "Temp Record"}}, "Pat")["id"]
        self.svc.create_tie({"fields": {"from": "pi", "to": rid, "type": "met", "evidence": "Test"}}, "Pat")
        res = self.svc.archive("contacts", rid, "Pat")
        self.assertEqual(res["cascaded"], 1)
        self.assertTrue(self.svc.validation()["current"]["ok"])
        self.assertIn(rid, self.svc.nb.archived["contacts"])

    def test_truncated_log_line_is_quarantined(self):
        self.svc.create_contact({"fields": {"name": "Kept Person"}}, "Pat")
        with open(self.svc.ws.log_path, "a", encoding="utf-8") as f:
            f.write('{"op": "contact.create", "id": "half')
        again = reopen(self.svc)
        self.assertIn("kept-person", again.nb.records["contacts"])
        self.assertEqual(len(again.ws.quarantined), 1)


class ImportExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.svc = fresh()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def edit_some(self, svc):
        rid = svc.create_contact({"fields": {"name": "Quinn Newperson", "institution": "Hill College",
                                             "role": "Professor"}}, "Pat")["id"]
        svc.update_contact("c1", {"set": {"role": "Full Professor"}}, "Pat")
        svc.create_tie({"fields": {"from": "pi", "to": rid, "type": "collaborator", "provenance": "reported",
                                   "evidence": "Pat reported a joint project."}}, "Pat")
        svc.create_note({"fields": {"contactId": rid, "metBy": "Pat", "context": "Workshop"}}, "Pat")
        return rid

    def bundle_bytes(self, svc):
        return exporter.export(svc, "bundle")[2]

    def second_workspace(self):
        from labnetwork.store import Workspace as WS
        ws2 = WS.create(os.path.join(self.tmp, "ws2"), self.svc.packet.path, lab_name="Copy")
        return Service(ws2)

    def test_bundle_round_trip_is_exact(self):
        self.edit_some(self.svc)
        data = self.bundle_bytes(self.svc)
        other = self.second_workspace()
        imp = Importer(other)
        prev = imp.preview("bundle.json", data)
        self.assertEqual(prev["mode"], "history")
        self.assertEqual(prev["needsDecision"], [])
        imp.apply(prev["batchId"], {}, "Pat")
        self.assertEqual(snapshot(other), snapshot(self.svc))
        self.assertEqual(other.person("c1", {})["history"][-1]["changes"][0]["to"], "Full Professor")
        # Importing the same file again changes nothing and is reported as already imported.
        prev2 = Importer(other).preview("bundle.json", data)
        self.assertEqual(len(prev2["alreadyImported"]), 1)
        self.assertEqual(set(prev2["summary"]["events"]), {"unchanged"})
        before = len(other.nb.events)
        Importer(other).apply(prev2["batchId"], {}, "Pat")
        self.assertEqual(len(other.nb.events), before + 1)  # only the batch record itself
        self.assertEqual(snapshot(other), snapshot(self.svc))

    def test_a_bundle_too_large_for_the_app_imports_from_the_command_line(self):
        self.edit_some(self.svc)
        data = self.bundle_bytes(self.svc)
        path = os.path.join(self.tmp, "bundle.json")
        with open(path, "wb") as f:
            f.write(data)
        other = self.second_workspace()
        with mock.patch("labnetwork.importer.MAX_BYTES", len(data) - 1):
            with self.assertRaises(ImportError_) as refused:
                Importer(other).preview("bundle.json", data)          # as the app receives an upload
            self.assertIn("python3 labnetwork.py import", str(refused.exception))
            # The command line writes only while the app is stopped (its lock names a live process).
            with open(other.ws.lock_path, "w", encoding="utf-8") as f:
                json.dump({"pid": os.getppid(), "port": 8765}, f)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
                self.assertEqual(cli.main(["import", other.ws.path, path, "--yes"]), 1)
            self.assertIn("Stop it first", err.getvalue())
            os.remove(other.ws.lock_path)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["import", other.ws.path, path, "--yes"]), 0)
        self.assertEqual(snapshot(reopen(other)), snapshot(self.svc))

    def test_bundle_conflict_is_explicit(self):
        self.svc.update_contact("c1", {"set": {"role": "Full Professor"}}, "Pat")
        data = self.bundle_bytes(self.svc)
        other = self.second_workspace()
        other.update_contact("c1", {"set": {"role": "Department Chair"}}, "Sam")
        prev = Importer(other).preview("bundle.json", data)
        self.assertEqual(prev["summary"]["events"].get("conflict"), 1)
        Importer(other).apply(prev["batchId"], {}, "Sam")
        self.assertEqual(other.nb.contact("c1")["role"], "Department Chair")   # not overwritten
        conf = other.conflicts({})["conflicts"]
        self.assertEqual(len(conf), 1)
        self.assertEqual((conf[0]["current"], conf[0]["incoming"]), ("Department Chair", "Full Professor"))
        other.resolve_conflict(conf[0]["conflictId"], {"resolution": "use-incoming"}, "Sam")
        self.assertEqual(other.nb.contact("c1")["role"], "Full Professor")
        self.assertEqual(other.conflicts({})["conflicts"], [])

    def test_notebook_export_reimports_as_unchanged(self):
        self.edit_some(self.svc)
        data = exporter.export(self.svc, "notebook")[2]
        prev = Importer(self.svc).preview("notebook.json", data)
        self.assertEqual(prev["mode"], "records")
        statuses = {s for coll in prev["summary"].values() for s in coll}
        self.assertEqual(statuses, {"unchanged"}, prev["summary"])

    def test_contacts_csv(self):
        csv_text = ("id,name,institution,role,website,researchSummary,researchSourceUrl\n"
                    "c1,Chris Coauthor,Lakeside University,Associate Professor,https://chris.example.org,"
                    "Studies sleep,https://example.org/c1\n"
                    "c2,Casey Coauthor,Hill College,,,,\n"
                    "c3,Cory Bridge,Other University,,,,\n"
                    ",Jordan Brandnew,Hill College,Lecturer,,,\n"
                    ",Morgan Mentor,Northfield Institute,Professor,,,\n"
                    ",,No Name University,,,,\n"
                    ",Bad Url Person,Somewhere,,www.no-scheme.org,,\n")
        imp = Importer(self.svc)
        prev = imp.preview("contacts.csv", csv_text.encode())
        by_row = {it["row"]: it for it in prev["items"]}
        self.assertEqual(by_row[2]["status"], "update")            # fills website + research summary
        self.assertEqual(by_row[3]["status"], "update")            # fills empty institution
        self.assertEqual(by_row[4]["status"], "conflict")          # institution differs
        self.assertEqual(by_row[5]["status"], "new")
        self.assertEqual(by_row[6]["status"], "possible-duplicate")
        self.assertEqual(by_row[7]["status"], "rejected")
        self.assertEqual(by_row[8]["status"], "rejected")
        self.assertIn("website", " ".join(by_row[8]["errors"]))
        with self.assertRaises(ImportError_):
            imp.apply(prev["batchId"], {}, "Pat")                   # row 6 needs a decision
        prev = imp.preview("contacts.csv", csv_text.encode())
        res = imp.apply(prev["batchId"], {"contacts:6": "merge:m1"}, "Pat")
        self.assertEqual(res["applied"]["created"], 1)
        self.assertEqual(res["applied"]["conflictsOpened"], 1)
        self.assertEqual(self.svc.nb.contact("c3")["institution"], "Lakeside University")
        self.assertEqual(self.svc.nb.contact("c2")["institution"], "Hill College")
        # Same file again: nothing new, no duplicate conflicts.
        prev = imp.preview("contacts.csv", csv_text.encode())
        by_row = {it["row"]: it for it in prev["items"]}
        self.assertEqual(by_row[4]["status"], "unchanged")
        self.assertEqual(by_row[5]["status"], "possible-duplicate")  # now matches the created record
        self.assertEqual(len(prev["alreadyImported"]), 1)

    def test_notes_and_ties_csv(self):
        notes = ("contactName,metBy,date,context,learning,followUp,recordedBy\n"
                 "Gale Grantee,Pat Investigator,2026-10-01,Grant review panel,Shared interests,Send aims,Pat\n"
                 "Nobody Known,Pat,,Somewhere,,,\n"
                 "Gale Grantee,Pat,2026-02-31,Bad date,,,\n")
        prev = Importer(self.svc).preview("notes.csv", notes.encode())
        st = {it["row"]: it["status"] for it in prev["items"]}
        self.assertEqual(st, {2: "new", 3: "rejected", 4: "rejected"})
        Importer(self.svc).apply(prev["batchId"], {}, "Pat")
        prev = Importer(self.svc).preview("notes.csv", notes.encode())
        self.assertEqual({it["row"]: it["status"] for it in prev["items"]}[2], "duplicate")
        ties = ("from,to,type,provenance,evidence,sourceUrl\n"
                "pi,g1,collaborator,reported,Joint pilot study,\n"
                "pi,ghost-person,coauthor,public,Paper,https://example.org/p\n"
                "pi,c1,coauthor,public,pi coauthor c1,https://example.org/source\n")
        prev = Importer(self.svc).preview("ties.csv", ties.encode())
        st = {it["row"]: it["status"] for it in prev["items"]}
        self.assertEqual(st, {2: "new", 3: "rejected", 4: "duplicate"})
        res = Importer(self.svc).apply(prev["batchId"], {}, "Pat")
        self.assertEqual(res["applied"]["created"], 1)
        new_tie = [e for e in self.svc.nb.records["edges"].values() if e["evidence"] == "Joint pilot study"][0]
        self.assertEqual((new_tie["status"], new_tie["confidence"]), ("proposed", "unverified"))
        self.assertNotIn(new_tie["id"], self.svc.graph().edges)   # proposed: not counted by default

    def test_csv_exports_have_headers_and_formula_guard(self):
        self.svc.update_contact("c1", {"set": {"notes": "=HYPERLINK(\"x\")"}}, "Pat")
        for fmt in ("contacts-csv", "notes-csv", "ties-csv"):
            name, ctype, data = exporter.export(self.svc, fmt)
            self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
            self.assertIn(b"id,", data[:10])


if __name__ == "__main__":
    unittest.main()
